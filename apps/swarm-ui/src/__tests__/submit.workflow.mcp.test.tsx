// MCP-UI-WF on the workflow form (docs/design/registered-mcp-servers.md §4.3
// "Workflows", §4.11; part of #1433): each step names its own MCP servers in
// its `input.mcp_servers`, and the workflow carries ONE top-level
// `mcp_credentials` map, because the credential is the caller's for the whole
// submission and every step naming a `dispatch` server uses that one value.
//
//   * a step's picker is drawn only when its profile DECLARES `mcp_servers`;
//     a step on any other profile shows none (the API would refuse the key);
//   * a `dispatch` server picked by any step asks for its credential ONCE,
//     for the whole workflow, however many steps name it;
//   * the credential travels beside `steps`, never inside a step's `input`
//     (stored and shown), and is not on the page once the POST is answered.
//
// MUTATIONS, each of which turns a case red: draw the picker for a step whose
// profile does not declare the input; put the credential in a step's input;
// send one map entry per step; leave the field filled after the 201.
//
// The credential is built at runtime; nothing here is credential-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity } from '../types'

const mocks = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadMcpServers: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  mocks.loadCapacity.mockImplementation(actual.loadCapacity)
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, loadCapacity: mocks.loadCapacity, USE_FIXTURES: false }
})

vi.mock('../McpServers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../McpServers')>()
  return { ...actual, loadMcpServers: mocks.loadMcpServers }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch

// Built at runtime, so no line of this file is a credential.
const CREDENTIAL = ['run', 'scoped', Math.random().toString(36).slice(2), Date.now().toString(36)].join('-')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function reg(name: string, over: Record<string, unknown> = {}) {
  return {
    server_id: `mcp_${name}`, tenant_id: 'eng', name, url: `https://${name}.example.com/mcp`,
    transport: 'streamable-http', description: `the ${name} server`,
    auth: { mode: 'dispatch', header: 'Authorization', scheme: 'Bearer' },
    allowed_profiles: [], allowed_tools: [], limits: {}, enabled: true, revision: 1, consumer: `mcp:${name}`,
    created_at: null, created_by: null, updated_at: null, updated_by: null, last_attached_at: null,
    ...over,
  }
}

const SERVERS = [
  reg('kb'),
  reg('wiki', { auth: { mode: 'none', header: 'Authorization', scheme: 'Bearer' } }),
  reg('retired', { enabled: false }),
]

/** The fixture's capacity, with `claude-code` declaring `mcp_servers`; `mock` declares nothing. */
async function capacity(): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const p = data.runner_profiles['claude-code']! as unknown as Record<string, unknown>
  p.input_contract = {
    required_keys: ['prompt'],
    inputs: { mcp_servers: { kind: 'list', minimum: 1, maximum: 4, means: 'registered MCP servers, by name' } },
  }
  return ok(data)
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/workflows')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({
        workflow: { workflow_id: 'wf_mcp_1', steps: [], created_at: '2026-10-18T12:00:00Z' },
        dispatch: null,
        mcp_credentials: [{ server: 'kb', expires_at: '2026-10-18T13:00:00Z' }],
      }), { status: 201, headers: JSON_HEADERS })
    }
    throw new Error(`unexpected request ${String(input)}`)
  }) as unknown as typeof fetch
  return posts
}

const stepCards = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.wfb-step'))

/** Give step `i` a runner, and a prompt when the runner asks for one. */
function choose(root: HTMLElement, i: number, profile: string, prompt?: string) {
  const step = stepCards(root)[i]!
  fireEvent.change(step.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: profile } })
  if (prompt !== undefined) fireEvent.change(stepCards(root)[i]!.querySelector<HTMLTextAreaElement>('textarea')!, { target: { value: prompt } })
}

async function form(): Promise<HTMLElement> {
  const { container } = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  return container
}

const addToStage = () => fireEvent.click(screen.getByRole('button', { name: '+ Add a step to this stage' }))
const go = () => screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement

beforeEach(async () => {
  mocks.loadCapacity.mockResolvedValue(await capacity())
  mocks.loadMcpServers.mockResolvedValue(ok({ tenant_id: 'eng', can_manage: false, servers: SERVERS, egress_addresses: null }))
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the workflow form names MCP servers per step', () => {
  it('draws no picker for a step whose profile does not declare `mcp_servers`', async () => {
    const root = await form()
    choose(root, 0, 'mock')
    expect(within(stepCards(root)[0]!).queryByRole('group', { name: 'MCP servers' })).toBeNull()
    expect(mocks.loadMcpServers).not.toHaveBeenCalled()
  })

  it('draws the picker only on the step whose profile declares it, offering the enabled registrations', async () => {
    const root = await form()
    addToStage()
    choose(root, 0, 'claude-code', 'ask the knowledge base')
    choose(root, 1, 'mock')
    const group = await within(stepCards(root)[0]!).findByRole('group', { name: 'MCP servers' }, WAIT)
    expect(within(group).getAllByRole('checkbox').map((c) => (c as HTMLInputElement).value)).toEqual(['kb', 'wiki'])
    expect(within(stepCards(root)[1]!).queryByRole('group', { name: 'MCP servers' })).toBeNull()
  })

  it("sends each step's servers in its input and ONE credentials map beside the steps, then forgets it", async () => {
    const posts = capturePosts()
    const root = await form()
    addToStage()
    addToStage()
    choose(root, 0, 'claude-code', 'read the decisions')
    choose(root, 1, 'claude-code', 'write the summary back')
    choose(root, 2, 'mock')
    const first = await within(stepCards(root)[0]!).findByRole('group', { name: 'MCP servers' }, WAIT)
    const second = await within(stepCards(root)[1]!).findByRole('group', { name: 'MCP servers' }, WAIT)
    fireEvent.click(within(first).getByRole('checkbox', { name: /kb/ }))
    fireEvent.click(within(first).getByRole('checkbox', { name: /wiki/ }))
    fireEvent.click(within(second).getByRole('checkbox', { name: /kb/ }))

    // Two steps name kb; its credential is asked for once, for the workflow.
    const fields = screen.getAllByLabelText(/credential for kb/i) as HTMLInputElement[]
    expect(fields).toHaveLength(1)
    expect(fields[0]!.type).toBe('password')
    expect(go().disabled, 'a dispatch server with no credential can still be sent').toBe(true)
    fireEvent.change(fields[0]!, { target: { value: CREDENTIAL } })
    expect(go().disabled).toBe(false)

    fireEvent.click(go())
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    const body = posts[0]!
    const steps = body.steps as Array<Record<string, unknown>>
    expect(steps[0]!.input).toEqual({ prompt: 'read the decisions', mcp_servers: ['kb', 'wiki'] })
    expect(steps[1]!.input).toEqual({ prompt: 'write the summary back', mcp_servers: ['kb'] })
    expect(steps[2]!.input).toEqual({})
    expect(body.mcp_credentials).toEqual({ kb: { value: CREDENTIAL, expires_in: 3600 } })
    expect(JSON.stringify(steps)).not.toContain(CREDENTIAL)

    await screen.findByRole('button', { name: 'Submit another' }, WAIT)
    const values = Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea')).map((i) => i.value)
    expect(values).not.toContain(CREDENTIAL)
    expect(document.body.innerHTML).not.toContain(CREDENTIAL)
  })

  it('sends no `mcp_servers` and no `mcp_credentials` when no step picks a server', async () => {
    const posts = capturePosts()
    const root = await form()
    choose(root, 0, 'claude-code', 'nothing to look up')
    await within(stepCards(root)[0]!).findByRole('group', { name: 'MCP servers' }, WAIT)
    fireEvent.click(go())
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect((posts[0]!.steps as Array<Record<string, unknown>>)[0]!.input).toEqual({ prompt: 'nothing to look up' })
    expect('mcp_credentials' in posts[0]!).toBe(false)
  })
})
