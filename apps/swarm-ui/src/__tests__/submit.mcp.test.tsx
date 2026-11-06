// MCP-UI on Submit (docs/design/registered-mcp-servers.md §4.3, §4.11; part of
// #1433): a task's form offers the workspace's registered MCP servers by name.
//
//   * the picker is drawn only for a profile that DECLARES `mcp_servers` in
//     its served `input_contract.inputs` (contract request 88): any other
//     profile refuses the key with 422 `invalid_input`;
//   * it offers the ENABLED registrations this profile may use, by name, and
//     nothing else: a disabled server, or one whose `allowed_profiles` leaves
//     this profile out, is not a choice (the API would refuse it 422
//     `mcp_server_unregistered` / 403 `mcp_server_profile_refused`);
//   * a picked `dispatch` server asks for its per-run credential, which
//     travels as the top-level `mcp_credentials` map -- never inside `input`,
//     which is stored and shown -- and which is NEVER RENDERED BACK: once the
//     POST has been answered the field is empty and the value is nowhere on
//     the page.
//
// MUTATIONS, each of which turns a case red: offer a disabled server; drop the
// `allowed_profiles` filter; put the credential inside `input`; leave the
// field filled after the 201.
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
const { SubmitScreen } = await import('../Submit')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
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
    created_at: null, created_by: null, updated_at: null, updated_by: null,
    ...over,
  }
}

const SERVERS = [
  reg('kb'),
  reg('wiki', { auth: { mode: 'none', header: 'Authorization', scheme: 'Bearer' } }),
  reg('retired', { enabled: false }),
  reg('codex-only', { allowed_profiles: ['codex'] }),
]

/** The fixture's capacity, with `claude-code` declaring `mcp_servers` as asked. */
async function capacity(declare: boolean): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const p = data.runner_profiles['claude-code']! as unknown as Record<string, unknown>
  p.input_contract = {
    required_keys: ['prompt'],
    ...(declare ? { inputs: { mcp_servers: { kind: 'list', minimum: 1, maximum: 4, means: 'registered MCP servers, by name' } } } : {}),
  }
  return ok(data)
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/tasks')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      // The API's echo: names and expiry, never the value (§4.3 "The response").
      return new Response(JSON.stringify({
        task: { id: 't_mcp_1', state: 'READY', runner_profile: 'claude-code', input: { prompt: 'p', mcp_servers: ['kb'] } },
        mcp_credentials: [{ server: 'kb', expires_at: '2026-10-18T13:00:00Z' }],
        scheduler_woken: true,
      }), { status: 201, headers: JSON_HEADERS })
    }
    throw new Error(`unexpected request ${String(input)}`)
  }) as unknown as typeof fetch
  return posts
}

async function form() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code"]')!)
  const prompt = r.container.querySelector<HTMLTextAreaElement>('.sbf-build textarea')!
  fireEvent.change(prompt, { target: { value: 'ask the knowledge base what changed last week' } })
  return r.container
}

const picker = () => screen.queryByRole('group', { name: 'MCP servers' })

beforeEach(() => {
  mocks.loadMcpServers.mockResolvedValue(ok({ tenant_id: 'eng', can_manage: false, servers: SERVERS, egress_addresses: null }))
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the MCP server picker', () => {
  it('is not drawn for a profile that does not declare `mcp_servers`', async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(false))
    await form()
    expect(picker()).toBeNull()
    expect(mocks.loadMcpServers).not.toHaveBeenCalled()
  })

  it('offers the enabled registrations this profile may use, by name, and not a disabled one', async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(true))
    await form()
    const group = await screen.findByRole('group', { name: 'MCP servers' }, WAIT)
    const offered = within(group).getAllByRole('checkbox').map((c) => (c as HTMLInputElement).value)
    expect(offered).toEqual(['kb', 'wiki'])
    expect(visible(group)).not.toContain('retired')
    expect(visible(group)).not.toContain('codex-only')
  })

  it('asks for a credential only for a `dispatch` server, and holds the send until it is given', async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(true))
    await form()
    const group = await screen.findByRole('group', { name: 'MCP servers' }, WAIT)
    fireEvent.click(within(group).getByRole('checkbox', { name: /wiki/ }))
    expect(group.querySelector('input[type="password"]')).toBeNull()
    fireEvent.click(within(group).getByRole('checkbox', { name: /kb/ }))
    const field = within(group).getByLabelText(/credential for kb/i) as HTMLInputElement
    expect(field.type).toBe('password')
    expect((screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(field, { target: { value: CREDENTIAL } })
    expect((screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('sends the names in `input` and the credential beside it, and never renders the credential back', async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(true))
    const posts = capturePosts()
    const container = await form()
    const group = await screen.findByRole('group', { name: 'MCP servers' }, WAIT)
    fireEvent.click(within(group).getByRole('checkbox', { name: /kb/ }))
    fireEvent.change(within(group).getByLabelText(/credential for kb/i), { target: { value: CREDENTIAL } })
    fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)

    const body = posts[0]!
    expect(body.input).toEqual({ prompt: 'ask the knowledge base what changed last week', mcp_servers: ['kb'] })
    expect(body.mcp_credentials).toEqual({ kb: { value: CREDENTIAL, expires_in: 3600 } })
    expect(JSON.stringify(body.input)).not.toContain(CREDENTIAL)

    await screen.findByRole('button', { name: 'Submit another' }, WAIT)
    // Not in any field's value, and not in any text the page draws.
    const values = Array.from(container.ownerDocument.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea')).map((i) => i.value)
    expect(values).not.toContain(CREDENTIAL)
    expect(document.body.innerHTML).not.toContain(CREDENTIAL)
    const field = screen.queryByLabelText(/credential for kb/i) as HTMLInputElement | null
    if (field !== null) expect(field.value).toBe('')
  })

  it('sends no `mcp_servers` and no `mcp_credentials` when nothing is picked', async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(true))
    const posts = capturePosts()
    await form()
    await screen.findByRole('group', { name: 'MCP servers' }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.input).toEqual({ prompt: 'ask the knowledge base what changed last week' })
    expect('mcp_credentials' in posts[0]!).toBe(false)
  })
})
