// MR-UI (docs/design/multi-repository-steps.md §4.1, §6 row 4; part of #1434):
// the console picks a step's additional repositories BY NAME, shows the commit
// each was pinned to, and lets an admin edit the two limits.
//
//   * the picker is drawn only for a runner that takes them (a GKE profile,
//     §4.3, owner's Q4) and offers only the workspace's registrations a task
//     may read: registered AND `readable_by_tasks === true` AND not archived
//     (owner's Q1) -- never `GET /v1/repositories/readable`, whose entries the
//     API refuses `repository_not_registered`;
//   * the body carries `owner/repo` names, a ref and a directory only when
//     typed, and nothing at all when nothing is picked;
//   * the workflow form sends ONE workflow-level list, and `[]` on a step
//     whose runner takes none, so that step does not inherit it;
//   * a refusal naming the field (`too_many_additional_repositories`, which
//     names the CURRENT limit) is printed under the picker;
//   * the 201 read-back and the agent detail show each pinned commit;
//   * Admin › Pool limits reads `/v1/admin/settings` and PUTs only the limits
//     that changed, and says what the response stored.
//
// MUTATIONS, each of which turns a case red: offer a registration with
// `readable_by_tasks` false or archived; read `/v1/repositories/readable`;
// send `ref: ""`; drop the `[]` on the Cloud Run step; drop the detail row;
// PUT both limits when one changed.
//
// No credential-shaped literal: the commits are built at runtime.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Task } from '../types'
import { at, ev, task } from './runfixture'

const mocks = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

// THE CARD'S READ GOES TO FETCH. Since QAFIX4-FIXTURE-A the card reads through
// loadPlatformSettings, which the fixture build (this file's build: the task
// form needs the fixture capacity) answers itself. The Pool limits cases serve
// `/v1/admin/settings` through fetch, so the loader's live read stands in here.
// So does `fixtureRead` (QA1B-14): the task form's picker reads
// `/v1/repositories` through it in the fixture build, and these cases serve
// that route's pages through fetch.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  const { read, route } = await import('../fetch')
  mocks.loadCapacity.mockImplementation(actual.loadCapacity)
  return {
    // The submit answers through fetch here, not the fixture build's 201
    // (QA2-FIX-07, runs2-01 and runs2-02).
    ...actual, USE_FIXTURES: false, loadCapacity: mocks.loadCapacity, loadCheckpoints: mocks.loadCheckpoints, loadTaskLogs: mocks.loadTaskLogs,
    loadPlatformSettings: () => read<import('../types').PlatformSettings>(route('/v1/admin/settings'), () => false),
    fixtureRead: (target: import('../fetch').ApiRoute) => read<unknown>(target, () => false),
  }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitScreen } = await import('../Submit')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
const { Run } = await import('../AgentDetail')
const { AdditionalRepositoryLimitsCard } = await import('../AdminSettings')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch

/** A 40-hex commit, built at runtime. */
function commit(seed: number): string {
  return Array.from({ length: 40 }, (_, i) => ((seed * 7 + i * 5) % 16).toString(16)).join('')
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function registration(repository: string, over: Record<string, unknown> = {}) {
  const [owner, repo] = repository.split('/')
  return {
    repo_id: `repo_${repo}`, tenant_id: 'eng', forge: 'github', owner, repo, repository,
    repository_url: `https://github.com/${repository}.git`, default_branch: 'main',
    archived: false, readable_by_tasks: true, allowed_profiles: [], index: {}, graph: {}, selection_policy: {},
    ...over,
  }
}

// Two pages, so the picker is held to following `next_page_token`.
const PAGE_ONE = {
  repositories: [
    registration('acme/web'),
    registration('acme/closed', { readable_by_tasks: false }),
    registration('acme/old', { archived: true }),
  ],
  next_page_token: 'p2',
  tenant_id: 'eng',
}
const PAGE_TWO = {
  repositories: [registration('acme/sdk', { default_branch: 'trunk' }), registration('acme/unset', { readable_by_tasks: null })],
  next_page_token: null,
  tenant_id: 'eng',
}

/** The fixture capacity with the backends this lane depends on stated, not inherited. */
async function capacity(): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  data.runner_profiles['claude-code']!.backend = 'GKE_AUTOPILOT'
  data.runner_profiles['claude-code']!.input_contract = { required_keys: ['prompt'] } as never
  data.runner_profiles['mock']!.backend = 'CLOUD_RUN_JOB'
  return ok(data)
}

interface Call { method: string; url: string; body: unknown }
type Answer = { status: number; body: unknown } | null

/** Stub fetch: the registrations, and whatever `post` answers for a write. */
function serve(post: (url: string, body: unknown) => Answer = () => null): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, url, body })
    let r: Answer = null
    if (method === 'GET' && url === '/v1/repositories') r = { status: 200, body: PAGE_ONE }
    else if (method === 'GET' && url === '/v1/repositories?page_token=p2') r = { status: 200, body: PAGE_TWO }
    else if (method !== 'GET') r = post(url, body)
    if (r === null) throw new Error(`unexpected request ${method} ${url}`)
    return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

const PINNED = [
  { name: 'web', repository: 'acme/web', repo_id: 'repo_web', ref: 'release', commit: commit(1), path: '../repos/web', forge_credential_source: 'person' },
  { name: 'sdk', repository: 'acme/sdk', repo_id: 'repo_sdk', ref: 'trunk', commit: commit(2), path: '../repos/sdk', forge_credential_source: 'person' },
]

const picker = () => screen.queryByRole('group', { name: 'Additional repositories' })
const offered = (group: HTMLElement) => within(group).getAllByRole('checkbox').map((c) => (c as HTMLInputElement).value)

async function taskForm(profile: string): Promise<HTMLElement> {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${profile}"]`)!)
  const prompt = r.container.querySelector<HTMLTextAreaElement>('.sbf-build textarea')
  if (prompt !== null) fireEvent.change(prompt, { target: { value: 'which service owns the endpoint the web app calls?' } })
  return r.container
}

const taskButton = () => screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement

beforeEach(async () => {
  mocks.loadCapacity.mockResolvedValue(await capacity())
  mocks.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  mocks.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the task form picks additional repositories by name', () => {
  it('offers only registrations readable by tasks, from every page, and never the token-readable list', async () => {
    const calls = serve()
    await taskForm('claude-code')
    const group = await screen.findByRole('group', { name: 'Additional repositories' }, WAIT)
    await waitFor(() => expect(offered(group)).toEqual(['acme/sdk', 'acme/web']), WAIT)
    expect(calls.some((c) => c.url.startsWith('/v1/repositories/readable'))).toBe(false)
  })

  it('is not drawn for a Cloud Run runner, and nothing is read for it', async () => {
    const calls = serve()
    await taskForm('mock')
    expect(picker()).toBeNull()
    expect(calls.filter((c) => c.url.startsWith('/v1/repositories'))).toEqual([])
  })

  it('sends names, with a ref or directory only when typed, and shows each pinned commit from the 201', async () => {
    const calls = serve((url) => (url === '/v1/tasks' ? {
      status: 201,
      body: { task: { ...task({ id: 't_extra_1', state: 'READY' }), additional_repositories: PINNED }, scheduler_woken: true },
    } : null))
    await taskForm('claude-code')
    const group = await screen.findByRole('group', { name: 'Additional repositories' }, WAIT)
    await waitFor(() => expect(offered(group)).toHaveLength(2), WAIT)
    fireEvent.click(within(group).getByRole('checkbox', { name: /acme\/web/ }))
    fireEvent.click(within(group).getByRole('checkbox', { name: /acme\/sdk/ }))
    fireEvent.change(within(group).getByLabelText('ref of acme/web'), { target: { value: ' release ' } })
    fireEvent.change(within(group).getByLabelText('directory of acme/sdk'), { target: { value: 'sdk-js' } })
    fireEvent.click(taskButton())

    await screen.findByText('t_extra_1', {}, WAIT)
    const posts = calls.filter((c) => c.method === 'POST' && c.url === '/v1/tasks')
    expect(posts).toHaveLength(1)
    expect((posts[0]!.body as Record<string, unknown>).additional_repositories).toEqual([
      { repository: 'acme/web', ref: 'release' },
      { repository: 'acme/sdk', name: 'sdk-js' },
    ])
    expect(JSON.stringify(posts[0]!.body)).not.toContain('https://')

    const facts = screen.getByRole('list', { name: 'additional repositories' })
    for (const p of PINNED) {
      const row = facts.querySelector<HTMLElement>(`[data-additional-repository="${p.repository}"]`)!
      expect(row.textContent).toContain(`${p.repository} @ ${p.ref}`)
      expect(row.textContent).toContain(p.commit.slice(0, 12))
      expect(row.querySelector('code')!.getAttribute('title')).toBe(p.commit)
    }
  })

  it('sends no `additional_repositories` key when nothing is picked', async () => {
    const calls = serve((url) => (url === '/v1/tasks' ? {
      status: 201, body: { task: task({ id: 't_plain', state: 'READY' }), scheduler_woken: true },
    } : null))
    await taskForm('claude-code')
    await screen.findByRole('group', { name: 'Additional repositories' }, WAIT)
    fireEvent.click(taskButton())
    await screen.findByText('t_plain', {}, WAIT)
    const post = calls.find((c) => c.method === 'POST')!
    expect(Object.keys(post.body as object)).not.toContain('additional_repositories')
  })

  it("prints the API's limit refusal under the picker", async () => {
    const said = 'a step may read at most 1 additional repository (the platform setting max_additional_repositories); it names 2'
    serve((url) => (url === '/v1/tasks' ? {
      status: 422,
      body: { code: 'too_many_additional_repositories', message: said, detail: { field: 'additional_repositories', limit: 1, count: 2 } },
    } : null))
    await taskForm('claude-code')
    const group = await screen.findByRole('group', { name: 'Additional repositories' }, WAIT)
    await waitFor(() => expect(offered(group)).toHaveLength(2), WAIT)
    fireEvent.click(within(group).getByRole('checkbox', { name: /acme\/web/ }))
    fireEvent.click(within(group).getByRole('checkbox', { name: /acme\/sdk/ }))
    fireEvent.click(taskButton())
    await waitFor(() => expect(within(group).getByRole('alert').textContent).toBe(said), WAIT)
  })
})

describe('the workflow form sends one list, and `[]` on a step that cannot take it', () => {
  const stepCards = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.wfb-step'))
  function choose(root: HTMLElement, i: number, profile: string, prompt?: string) {
    fireEvent.change(stepCards(root)[i]!.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: profile } })
    if (prompt !== undefined) fireEvent.change(stepCards(root)[i]!.querySelector<HTMLTextAreaElement>('textarea')!, { target: { value: prompt } })
  }

  it('names the steps that read them, and sends the workflow list with `[]` on the Cloud Run step', async () => {
    const calls = serve((url) => (url === '/v1/workflows' ? {
      status: 201, body: { workflow: { workflow_id: 'wf_extra_1', steps: [], created_at: '2026-10-18T12:00:00Z' }, dispatch: null },
    } : null))
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    expect(picker(), 'drawn before any step takes them').toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '+ Add a step to this stage' }))
    choose(container, 0, 'claude-code', 'map the endpoints across the three repositories')
    choose(container, 1, 'mock')
    const group = await screen.findByRole('group', { name: 'Additional repositories' }, WAIT)
    await waitFor(() => expect(offered(group)).toEqual(['acme/sdk', 'acme/web']), WAIT)
    const readers = group.querySelector<HTMLElement>('[data-extra-readers]')!.textContent ?? ''
    expect(readers).toMatch(/^Read by claude-code\S*; not by mock\S*, whose runner takes none\.$/)

    fireEvent.click(within(group).getByRole('checkbox', { name: /acme\/web/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1), WAIT)
    const body = calls.find((c) => c.method === 'POST')!.body as { additional_repositories: unknown; steps: Array<Record<string, unknown>> }
    expect(body.additional_repositories).toEqual([{ repository: 'acme/web' }])
    const byProfile = Object.fromEntries(body.steps.map((s) => [s.runner_profile, s]))
    expect(Object.keys(byProfile['claude-code']!)).not.toContain('additional_repositories')
    expect(byProfile['mock']!.additional_repositories).toEqual([])
  })
})

describe('the agent detail shows each pinned commit', () => {
  it('draws one `reads` row per additional repository, with its commit', async () => {
    const t: Task = { ...task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) }), additional_repositories: PINNED }
    const r: AgentRun = {
      task: t, events: [ev('submitted', at(-1), null)], eventsDetail: null, attempts: [], attemptsDetail: null,
      classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } }, classesDetail: null,
      classesRouteMissing: false,
    }
    globalThis.fetch = vi.fn(async () => new Response('{}', { status: 404, headers: JSON_HEADERS })) as unknown as typeof fetch
    const { container } = render(<Run run={r} />)
    await waitFor(() => expect(container.querySelector('.dt-ifacts')).not.toBeNull(), WAIT)
    const rows = Array.from(container.querySelectorAll<HTMLElement>('.dt-ifacts [data-additional-repository]'))
    expect(rows.map((row) => row.dataset.additionalRepository)).toEqual(['acme/web', 'acme/sdk'])
    expect(rows[1]!.querySelector('code')!.getAttribute('title')).toBe(commit(2))
    expect(rows[1]!.textContent).toContain('../repos/sdk'.split('/').pop()!)
  })

  it('draws no row for a task that reads none', async () => {
    const r: AgentRun = {
      task: task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) }),
      events: [ev('submitted', at(-1), null)], eventsDetail: null, attempts: [], attemptsDetail: null,
      classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } }, classesDetail: null,
      classesRouteMissing: false,
    }
    globalThis.fetch = vi.fn(async () => new Response('{}', { status: 404, headers: JSON_HEADERS })) as unknown as typeof fetch
    const { container } = render(<Run run={r} />)
    await waitFor(() => expect(container.querySelector('.dt-ifacts')).not.toBeNull(), WAIT)
    expect(container.querySelector('[data-additional-repository]')).toBeNull()
  })
})

describe('Admin › Pool limits edits the two limits', () => {
  const SETTINGS = {
    merge_by_default: false, max_additional_repositories: 5, additional_repositories_budget_gib: 6,
    updated_at: null, updated_by: null, settings_document: 'missing',
  }

  function serveSettings(put: (body: Record<string, unknown>) => Answer): Call[] {
    const calls: Call[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ method, url, body })
      const r = url !== '/v1/admin/settings' ? null
        : method === 'GET' ? { status: 200, body: SETTINGS }
        : method === 'PUT' ? put(body as Record<string, unknown>) : null
      if (r === null) throw new Error(`unexpected request ${method} ${url}`)
      return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
    }) as unknown as typeof fetch
    return calls
  }

  const count = () => screen.getByLabelText('Most additional repositories per step') as HTMLInputElement
  const budget = () => screen.getByLabelText('Size budget per step, GiB') as HTMLInputElement
  const save = () => screen.getByRole('button', { name: 'Save limits' }) as HTMLButtonElement

  it('reads the current values, and PUTs only the one that changed', async () => {
    const calls = serveSettings((body) => ({ status: 200, body: { ...SETTINGS, ...body, settings_document: 'present', updated_by: 'admin@example.com' } }))
    render(<AdditionalRepositoryLimitsCard />)
    await waitFor(() => expect(count().value).toBe('5'), WAIT)
    expect(budget().value).toBe('6')
    expect(save().disabled, 'nothing changed yet').toBe(true)

    fireEvent.change(count(), { target: { value: '7' } })
    fireEvent.click(save())
    const status = await screen.findByRole('status', {}, WAIT)
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([{ max_additional_repositories: 7 }])
    expect(status.textContent).toContain('at most 7 additional repositories per step, 6 GiB in total')
  })

  it('PUTs both when both change', async () => {
    const calls = serveSettings((body) => ({ status: 200, body: { ...SETTINGS, ...body, settings_document: 'present' } }))
    render(<AdditionalRepositoryLimitsCard />)
    await waitFor(() => expect(count().value).toBe('5'), WAIT)
    fireEvent.change(count(), { target: { value: '10' } })
    fireEvent.change(budget(), { target: { value: '12' } })
    fireEvent.click(save())
    await screen.findByRole('status', {}, WAIT)
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([
      { max_additional_repositories: 10, additional_repositories_budget_gib: 12 },
    ])
  })

  it("sends nothing for a value that is not a whole number, and shows the API's range refusal", async () => {
    const calls = serveSettings(() => ({
      status: 422, body: { code: 'validation_failed', message: 'max_additional_repositories is an integer from 1 to 10' },
    }))
    render(<AdditionalRepositoryLimitsCard />)
    await waitFor(() => expect(count().value).toBe('5'), WAIT)
    fireEvent.change(count(), { target: { value: '0' } })
    expect(save().disabled).toBe(true)
    fireEvent.change(count(), { target: { value: '11' } })
    fireEvent.click(save())
    expect(await screen.findByText(/an integer from 1 to 10/, {}, WAIT)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([{ max_additional_repositories: 11 }])
  })

  it('says a non-admin cannot read them, and draws no field', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ code: 'admin_required', message: 'admins only' }),
      { status: 403, headers: JSON_HEADERS })) as unknown as typeof fetch
    render(<AdditionalRepositoryLimitsCard />)
    expect(await screen.findByText('Platform admins only.', {}, WAIT)).toBeTruthy()
    expect(screen.queryByLabelText('Most additional repositories per step')).toBeNull()
  })

  // Pool limits is held to 40 words (prose.operator.test.tsx), so a failed read
  // is drawn as two words and the message it carries is kept in `title`.
  it('says the limits were not read, keeping the message one hover away, and draws no field', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ code: 'internal', message: 'settings store timed out' }),
      { status: 500, headers: JSON_HEADERS })) as unknown as typeof fetch
    render(<AdditionalRepositoryLimitsCard />)
    const line = await screen.findByRole('alert', {}, WAIT)
    expect(line.textContent).toBe('Not read')
    expect(line.getAttribute('title')).toContain('settings store timed out')
    expect(screen.queryByLabelText('Most additional repositories per step')).toBeNull()
  })
})
