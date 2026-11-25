// THE CONSOLE CALLS ONLY PATHS SWARM-API SERVES, AND THE API'S CONSOLE LINKS
// OPEN CONSOLE ROUTES (part of #72).
//
// Both ends of each seam used to be compared by Python tests that read this
// app's source as text: /v1/accounts literals in api.ts and Accounts.tsx, the
// /v1 literals in CheckpointBrowser.tsx, LEDGER_SECTIONS in Activity.tsx, and
// the link spellings in paths.ts. A literal is not a request: a path built
// another way, or a literal in a comment, fooled them both ways. Here the
// other end is `generated/constants/routes.json`, which
// scripts/api_constants/routes.py writes from swarm-api's own OpenAPI document
// and its own link builders (`gen_api_types.py --check` fails while it is
// stale), and this end is the real request functions, driven over a stubbed
// fetch: every method and path they request must be a generated route.
//
// Nothing here reads a source file. Placeholders only.

import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import routes from '../generated/constants/routes.json'
import { addressToPath, pathToAddress } from '../paths'

type Request = { method: string; path: string; query: URLSearchParams }

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

/** `^...$` for an OpenAPI path template: each `{param}` is one path segment. */
function templatePattern(template: string): RegExp {
  const escape = (s: string) => s.replace(/[.*+?^$()|[\]\\]/g, '\\$&')
  return new RegExp('^' + template.split(/\{[^{}]+\}/).map(escape).join('[^/]+') + '$')
}

const SERVED = routes.api_routes.map((r) => ({ ...r, pattern: templatePattern(r.path) }))

/** The generated route a request is to, or null when swarm-api serves no such method and path. */
function servedRoute(method: string, path: string): { method: string; path: string } | null {
  const hit = SERVED.find((r) => r.method === method.toUpperCase() && r.pattern.test(path))
  return hit === undefined ? null : { method: hit.method, path: hit.path }
}

/** The URL a fetch input names (a string, a URL or a Request). */
function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

/** Every request `run` makes, against the live (not fixture) code paths. */
async function requestsOf(run: (mod: { api: typeof import('../api') }) => Promise<unknown>): Promise<Request[]> {
  const seen: Request[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(urlOf(input), 'http://console.example.test')
      seen.push({ method: (init?.method ?? 'GET').toUpperCase(), path: url.pathname, query: url.searchParams })
      return Promise.resolve(new Response(JSON.stringify({ detail: 'stubbed' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }))
    }),
  )
  const api = await import('../api')
  try {
    await run({ api })
  } catch {
    // A refused answer may be thrown on; what was requested is recorded already.
  }
  return seen
}

/** The requests no generated route serves, as `METHOD path`. */
function unserved(requests: Request[]): string[] {
  return requests.filter((r) => servedRoute(r.method, r.path) === null).map((r) => `${r.method} ${r.path}`)
}

function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
}

describe('the route matcher', () => {
  it('matches a method and a filled-in template, and nothing else', () => {
    expect(routes.api_routes.length).toBeGreaterThan(50)
    expect(servedRoute('GET', '/v1/accounts')).toEqual({ method: 'GET', path: '/v1/accounts' })
    expect(servedRoute('PUT', '/v1/accounts/eng%3Amain/name')).toEqual({
      method: 'PUT',
      path: '/v1/accounts/{account_id}/name',
    })
    // MUTATION: ignore the method, or let a placeholder span segments.
    expect(servedRoute('PATCH', '/v1/accounts')).toBeNull()
    expect(servedRoute('GET', '/v1/accounts/a/b/c/d/e')).toBeNull()
    expect(servedRoute('GET', '/v1/no-such-route')).toBeNull()
  })

  it('lists every route once, sorted by path then method', () => {
    const keys = routes.api_routes.map((r) => `${r.path} ${r.method}`)
    expect(new Set(keys).size).toBe(keys.length)
    // Plain code-unit order (`<`), as the generator writes it; `localeCompare` would reorder punctuation.
    const order = (a: string, b: string): number => {
      if (a === b) return 0
      return a < b ? -1 : 1
    }
    const byPathThenMethod = (a: { path: string; method: string }, b: { path: string; method: string }): number =>
      order(a.path, b.path) || order(a.method, b.method)
    expect([...routes.api_routes].sort(byPathThenMethod)).toEqual(routes.api_routes)
  })
})

// ---------------------------------------------------------------------------
// Accounts: every request function that reaches /v1/accounts
// ---------------------------------------------------------------------------

const ACCOUNT = 'eng:main'

/** api.ts's accounts request functions, each with sample arguments. */
const API_ACCOUNT_CALLS: Record<string, (api: typeof import('../api')) => Promise<unknown>> = {
  loadAccountsBoard: (a) => a.loadAccountsBoard(),
  beginAccountSignIn: (a) => a.beginAccountSignIn({ label: 'main', lend_to: [] }),
  finishAccountSignIn: (a) => a.finishAccountSignIn({ state: 'st_1', code: 'code_1' }),
  beginDeviceSignIn: (a) => a.beginDeviceSignIn({ label: 'main', lend_to: [] }),
  pollDeviceSignIn: (a) => a.pollDeviceSignIn({ state: 'st_1' }),
  setAccountLending: (a) => a.setAccountLending(ACCOUNT, ['research']),
  setAccountPerWorkspaceMax: (a) => a.setAccountPerWorkspaceMax(ACCOUNT, { eng: 2 }),
  setAccountModelClasses: (a) => a.setAccountModelClasses(ACCOUNT, ['standard']),
  setAccountName: (a) => a.setAccountName(ACCOUNT, 'Main'),
  setAccountMaxAgents: (a) => a.setAccountMaxAgents(ACCOUNT, 3),
  loadAccountHealth: (a) => a.loadAccountHealth(ACCOUNT),
  setAccountState: (a) => a.setAccountState(ACCOUNT, 'PAUSED', 'maintenance'),
  refreshAccount: (a) => a.refreshAccount(ACCOUNT),
  removeAccount: (a) => a.removeAccount(ACCOUNT),
  loadAccountHealthBoard: (a) => a.loadAccountHealthBoard(),
  loadApiKeys: (a) => a.loadApiKeys(),
  setKeySpendCap: (a) => a.setKeySpendCap(ACCOUNT, 50),
  setKeyPerWorkspaceMax: (a) => a.setKeyPerWorkspaceMax(ACCOUNT, { eng: 2 }),
  setKeyLends: (a) => a.setKeyLends(ACCOUNT, { research: { max_agents: 1, monthly_spend_cap_usd: 10 } }),
  loadAccountPool: (a) => a.loadAccountPool(),
}

/** Why the two key writers request no /v1/accounts path. */
const THROUGH_REDEEM_LINK = 'mints a one-time setup link and redeems it: no /v1/accounts path'

/**
 * api.ts exports whose name reads as an account's but that request no
 * /v1/accounts path, each with why: they are pure, or send through another
 * seam. A new accounts export must join one table or the other.
 */
const NOT_ACCOUNT_REQUESTS: Record<string, string> = {
  normaliseAccountHealth: 'pure: shapes a served health record',
  isApiKey: 'pure: classifies a listed account',
  normSignInClient: 'pure: shapes the admin sign-in client record',
  saveSignInClient: 'an /v1/admin/setup request, not an account one',
  addApiKey: THROUGH_REDEEM_LINK,
  replaceApiKey: THROUGH_REDEEM_LINK,
}

/** What names an accounts export of api.ts: the names the old literal scan's paths sat in. */
const ACCOUNTISH = /account|signin|apikey|^setKey/i

/** Accounts.tsx's own loaders and writers of /v1/accounts paths. */
const SCREEN_ACCOUNT_CALLS: Record<string, (s: typeof import('../Accounts')) => Promise<unknown>> = {
  loadAccountHolders: (s) => s.loadAccountHolders(ACCOUNT),
  loadAccountHistory: (s) => s.loadAccountHistory(ACCOUNT, 'cur_1'),
  setAccountPools: (s) => s.setAccountPools(ACCOUNT, ['default'], 'main'),
}

describe('every accounts request is to a route swarm-api serves', () => {
  it('drives every accounts export of api.ts, or says why it does not', async () => {
    live()
    const api = await import('../api')
    const exported = Object.entries(api)
      .filter(([name, v]) => typeof v === 'function' && ACCOUNTISH.test(name))
      .map(([name]) => name)
      .sort((a, b) => a.localeCompare(b))
    // MUTATION: drop a row from API_ACCOUNT_CALLS.
    const missing = exported.filter((n) => !(n in API_ACCOUNT_CALLS) && !(n in NOT_ACCOUNT_REQUESTS))
    expect(missing, 'an accounts export of api.ts this test does not drive').toEqual([])
    const stale = [...Object.keys(API_ACCOUNT_CALLS), ...Object.keys(NOT_ACCOUNT_REQUESTS)].filter((n) => !exported.includes(n))
    expect(stale, 'a table row names no accounts export of api.ts').toEqual([])
  })

  it('drives every accounts export of Accounts.tsx', async () => {
    live()
    const screenModule = await import('../Accounts')
    const exported = Object.entries(screenModule)
      .filter(([name, v]) => typeof v === 'function' && /^(load|set|begin|finish|poll|refresh|remove)Account/.test(name))
      .map(([name]) => name)
      .sort((a, b) => a.localeCompare(b))
    expect(exported).toEqual(Object.keys(SCREEN_ACCOUNT_CALLS).sort((a, b) => a.localeCompare(b)))
  })

  for (const [name, call] of Object.entries(API_ACCOUNT_CALLS)) {
    it(`api.ts ${name}`, async () => {
      live()
      const requests = await requestsOf(({ api }) => call(api))
      // Driven for real: it asked for at least one accounts path.
      expect(requests.some((r) => r.path.startsWith('/v1/accounts'))).toBe(true)
      // MUTATION: change a path in api.ts to one swarm-api does not serve.
      expect(unserved(requests)).toEqual([])
    })
  }

  for (const [name, call] of Object.entries(SCREEN_ACCOUNT_CALLS)) {
    it(`Accounts.tsx ${name}`, async () => {
      live()
      const requests = await requestsOf(async () => call(await import('../Accounts')))
      expect(requests.some((r) => r.path.startsWith('/v1/accounts'))).toBe(true)
      expect(unserved(requests)).toEqual([])
    })
  }
})

// ---------------------------------------------------------------------------
// The checkpoint browser
// ---------------------------------------------------------------------------

describe("the checkpoint browser's requests are to routes swarm-api serves", () => {
  it('lists, opens a file and links the download, all on served routes', async () => {
    live()
    const listing = {
      task_id: 'task_a',
      tenant_id: 'eng',
      attempt_id: 'att_1',
      checkpoint_id: 'ckpt-00001',
      prefix: 'tenants/eng/tasks/task_a/attempts/att_1/checkpoints/ckpt-00001',
      archive: { key: 'ckpt/archive.tar.gz', uri: 'gs://swarm-artifacts-example-project/ckpt/archive.tar.gz', bytes: 512 },
      manifest: {
        status: 'present',
        detail: null,
        file_count: 1,
        archive_bytes: 512,
        archive_sha256: 'abc',
        created_at: null,
        label: 'periodic',
        archive_key_agrees: true,
      },
      status: 'ok',
      detail: null,
      files: [{ path: 'state.json', size: 23, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false }],
      count: 1,
      truncated: false,
      truncated_reason: null,
      file_count_agrees: true,
      scanned_bytes: 512,
    }
    const seen: Request[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(urlOf(input), 'http://console.example.test')
        seen.push({ method: (init?.method ?? 'GET').toUpperCase(), path: url.pathname, query: url.searchParams })
        const body = url.pathname.endsWith('/files') ? listing : { detail: 'stubbed' }
        return Promise.resolve(new Response(JSON.stringify(body), {
          status: url.pathname.endsWith('/files') ? 200 : 404,
          headers: { 'content-type': 'application/json' },
        }))
      }),
    )
    const { CheckpointBrowser } = await import('../CheckpointBrowser')
    render(
      createElement(CheckpointBrowser, { taskId: 'task_a', attemptId: 'att_1', checkpointId: 'ckpt-00001', onClose: () => {} }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    await waitFor(() => expect(seen.some((r) => r.path.endsWith('/files/state.json'))).toBe(true))
    // The download is a link, not a fetch: its href is a request too.
    const links = screen.queryAllByRole('link').filter((a) => a.getAttribute('href')?.startsWith('/v1/') === true).map((a) => {
      const url = new URL(a.getAttribute('href')!, 'http://console.example.test')
      return { method: 'GET', path: url.pathname, query: url.searchParams }
    })
    expect(links.map((l) => l.path)).toContain('/v1/tasks/task_a/checkpoints/ckpt-00001/content')
    const requests = [...seen, ...links]
    // The listing, the file and the download: three routes, each one served.
    expect(new Set(requests.map((r) => servedRoute(r.method, r.path)?.path)).size).toBeGreaterThanOrEqual(3)
    // MUTATION: change a path in CheckpointBrowser.tsx to one swarm-api does not serve.
    expect(unserved(requests)).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// The outcome ledger
// ---------------------------------------------------------------------------

describe("the outcome ledger's reads are to a served route, with served sections", () => {
  it('asks for the headline sections, then the late ones, each one the API accepts', async () => {
    live()
    const { LEDGER_SECTIONS, LEDGER_LATE_SECTIONS } = await import('../Activity')
    // The headline's first read asks for neither section that costs reads of
    // its own; the second read asks for exactly those (archived issue 2422).
    expect(LEDGER_SECTIONS).not.toContain('coverage')
    expect(LEDGER_SECTIONS).not.toContain('previous')
    expect([...LEDGER_LATE_SECTIONS].sort((a, b) => a.localeCompare(b))).toEqual(['coverage', 'previous'])
    for (const sections of [LEDGER_SECTIONS, LEDGER_LATE_SECTIONS]) {
      const requests = await requestsOf(({ api }) => api.loadOutcomes(new URLSearchParams({ span: '7d' }), sections))
      expect(requests.map((r) => `${r.method} ${r.path}`)).toEqual(['GET /v1/outcomes'])
      expect(unserved(requests)).toEqual([])
      const asked = requests[0]!.query.getAll('section')
      expect(asked).toEqual([...sections])
      // MUTATION: add a section to LEDGER_SECTIONS that swarm-api's SECTIONS lacks.
      expect(asked.filter((s) => !routes.outcome_sections.includes(s))).toEqual([])
    }
  })
})

// ---------------------------------------------------------------------------
// Console links the API builds
// ---------------------------------------------------------------------------

describe('every console link the API builds opens a console route', () => {
  const ADDRESS_OF: Record<string, (id: string) => string> = {
    agent: (id) => `work/task/${id}`,
    workflow: (id) => `work/workflows?${new URLSearchParams({ wf: id })}`,
    run: (id) => `work/runs?${new URLSearchParams({ run: id })}`,
  }

  it('knows every link kind the API builds', () => {
    expect(Object.keys(routes.console_links).sort((a, b) => a.localeCompare(b))).toEqual(Object.keys(ADDRESS_OF).sort((a, b) => a.localeCompare(b)))
  })

  for (const [kind, template] of Object.entries(routes.console_links)) {
    it(`${kind}: ${template}`, () => {
      const id = `${kind}_1`
      const path = template.replace(/\{[a-z_]+\}/, id)
      expect(path).not.toMatch(/[{}]/)
      // MUTATION: rename the route in paths.ts, or change the path a codec.py builder returns.
      const resolved = pathToAddress(path)
      expect(resolved, `${path} is not a console route`).not.toBeNull()
      expect(resolved!.address).toBe(ADDRESS_OF[kind]!(id))
      // The console's own builder spells the same path for the same id, with
      // its default agent tab: the tab a pasted agent link opens in.
      expect(addressToPath(resolved!.address)).toBe(path)
    })
  }

  for (const [kind, example] of Object.entries(routes.console_link_examples)) {
    it(`${kind}: an id that needs encoding is spelled as the console spells it`, () => {
      const resolved = pathToAddress(example.path)
      expect(resolved).not.toBeNull()
      expect(resolved!.address).toBe(ADDRESS_OF[kind]!(example.id))
      // MUTATION: use Python's default quote() in codec.py (it escapes `(`, `)` and `!`).
      expect(addressToPath(resolved!.address)).toBe(example.path)
    })
  }
})
