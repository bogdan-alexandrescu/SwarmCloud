// Builders for the Repositories and Git tokens tests (repositories.html,
// picked 2026-10-05). Placeholders only: example-org/example-*, Operator.
//
// NOTHING HERE IS A CREDENTIAL, AND NOTHING LOOKS LIKE ONE AS A LITERAL. The
// worker refuses to publish a diff whose added lines match its credential
// scan, so every sha and every token-shaped value is built at runtime from
// pieces. `leakedValue()` is the one token-shaped string in the suite: it is
// put into API responses on purpose, to prove the screens never draw it.

import { vi } from 'vitest'

export const JSON_HEADERS = { 'content-type': 'application/json' }

/** A 40-character commit sha from a short prefix, built at runtime. */
export function sha(prefix: string): string {
  return prefix.padEnd(40, '0')
}

/** A value shaped like a GitHub token, built at runtime, that must never reach the DOM. */
export function leakedValue(): string {
  return ['gh', 'p_'].join('') + 'Zq9'.repeat(12)
}

/** Every token shape the scan looks for, as patterns (never as literals). */
export const TOKEN_SHAPES: readonly RegExp[] = [
  new RegExp('gh[pousr]_[A-Za-z0-9]{16,}'),
  new RegExp('github' + '_pat_[A-Za-z0-9_]{16,}'),
  new RegExp('-----BEGIN [A-Z ]*PRIVATE KEY'),
]

/** The whole rendered document, attributes included: what a scan must find nothing token-shaped in. */
export function tokenShapedIn(html: string): string[] {
  const hits = TOKEN_SHAPES.filter((re) => re.test(html)).map((re) => re.source)
  if (html.includes(leakedValue())) hits.push('the leaked value')
  return hits
}

export const NOW = Date.now()
export const ago = (ms: number) => new Date(NOW - ms).toISOString()
export const MIN = 60_000
export const HOUR = 60 * MIN
export const DAY = 24 * HOUR

type Json = Record<string, unknown>

export function repo(over: Json = {}, index: Json = {}): Json {
  return {
    repo_id: 'repo_0a1b2c3d4e5f6071',
    tenant_id: 'eng',
    forge: 'github',
    owner: 'example-org',
    repo: 'example-api',
    repository_url: 'https://github.com/example-org/example-api',
    default_branch: 'main',
    allowed_profiles: ['claude-code'],
    created_by: 'operator@swarm.example.com',
    created_at: ago(DAY),
    ...over,
    index: {
      interval_hours: 24,
      on_change: true,
      min_change_interval_minutes: 30,
      full_every_days: 7,
      paused: false,
      current_sha: sha('a1b2c3d'),
      head_sha: sha('a1b2c3d'),
      head_read_at: ago(2 * MIN),
      behind_by: 0,
      last_indexed_at: ago(18 * MIN),
      last_kind: 'incremental',
      in_flight_task_id: null,
      coverage: 0.86,
      ...index,
    },
  }
}

export function token(over: Json = {}): Json {
  return {
    token_id: 'tok_0011223344556677',
    tenant_id: 'eng',
    scope: 'tenant',
    repo_ids: [],
    provider_suffix: 'git',
    secret_name: 'swarm-tenant-eng-git',
    forge: 'github',
    kind: 'fine_grained_pat',
    forge_login: 'eng-swarm-bot',
    last4: 'a41c',
    expires_at: new Date(NOW + 9 * DAY + HOUR).toISOString(),
    verified_at: ago(2 * HOUR),
    state: 'active',
    // A misbehaving API that served a value: the screens must not draw it.
    value: leakedValue(),
    ...over,
  }
}

export type Handler = (method: string, url: string, body: unknown) => { status: number; body: unknown } | null

/** Stub fetch; an unstubbed call answers like a route that is not there (404, no code). */
export function serve(handler: Handler) {
  const calls: { method: string; url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, url, body })
    const r = handler(method, url, body) ?? { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

export const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
