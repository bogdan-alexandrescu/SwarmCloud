// fetch.ts as BEHAVIOUR.
//
// fetch.ts states its own purpose in its first line: "A failed request must
// never be representable as empty data." Until this file existed, nothing
// executed that claim. The Python source-grep tests could only check that the
// words were present in the file -- and the words are present in a comment
// either way.
//
// Every case below is a real `Response` handed to the real `read`/`write`, so
// what is under test is the shipped classification, not a restatement of it.

import { describe, expect, it, vi } from 'vitest'

import {
  errorHeading,
  errorReassurance,
  isPaused,
  num,
  probeSnapshot,
  read,
  write,
  type ApiErrorKind,
  type Result,
} from '../fetch'

/** One canned response, as the real `Response` the browser would hand us. */
function respond(
  body: string,
  init: { status?: number; contentType?: string | null; headers?: Record<string, string> } = {},
): void {
  const headers: Record<string, string> = { ...(init.headers ?? {}) }
  if (init.contentType !== null) headers['content-type'] = init.contentType ?? 'application/json'
  globalThis.fetch = vi.fn(
    async () => new Response(body, { status: init.status ?? 200, headers }),
  ) as unknown as typeof fetch
}

function rejects(message: string): void {
  globalThis.fetch = vi.fn(async () => {
    throw new TypeError(message)
  }) as unknown as typeof fetch
}

const never = (): boolean => false
const PREVIOUS = { data: { pools: ['kept'] }, fetchedAt: 1_000 }

// ---------------------------------------------------------------------------
// The headline property
// ---------------------------------------------------------------------------

/**
 * Every way a read can fail, as this module classifies them. The table is the
 * test: a new failure path added to `read` without a row here is a path nobody
 * has asserted never produces `ok` or `empty`.
 */
const FAILURES: ReadonlyArray<{
  name: string
  arrange: () => void
  kind: ApiErrorKind
  httpStatus: number | null
}> = [
  {
    name: 'the network never answered',
    arrange: () => rejects('Failed to fetch'),
    kind: 'unreachable',
    httpStatus: null,
  },
  {
    name: 'IAP answered 200 with its sign-in page',
    arrange: () => respond('<!doctype html><title>Sign in</title>', { contentType: 'text/html' }),
    kind: 'session_expired',
    httpStatus: 200,
  },
  {
    name: 'the session expired properly',
    arrange: () => respond('{"code":"unauthenticated","message":"no"}', { status: 401 }),
    kind: 'unauthenticated',
    httpStatus: 401,
  },
  {
    name: 'admin group membership is required',
    arrange: () =>
      respond('{"code":"forbidden","message":"Admin group membership is required."}', {
        status: 403,
      }),
    kind: 'admin_required',
    httpStatus: 403,
  },
  {
    name: 'the domain is not permitted',
    arrange: () =>
      respond('{"code":"forbidden","message":"Domain gmail.com is not permitted."}', {
        status: 403,
      }),
    kind: 'wrong_domain',
    httpStatus: 403,
  },
  {
    name: 'the tenant is disabled',
    arrange: () =>
      respond('{"code":"forbidden","message":"Tenant eng is disabled."}', { status: 403 }),
    kind: 'tenant_disabled',
    httpStatus: 403,
  },
  {
    name: 'nothing with that id, or it is another tenant’s',
    arrange: () => respond('{"code":"not_found","message":"no such task"}', { status: 404 }),
    kind: 'not_found',
    httpStatus: 404,
  },
  {
    name: 'it conflicts with what is registered',
    arrange: () => respond('{"code":"conflict","message":"already exists"}', { status: 409 }),
    kind: 'conflict',
    httpStatus: 409,
  },
  {
    name: 'the request body was refused',
    arrange: () => respond('{"detail":[{"loc":["body","x"]}]}', { status: 422 }),
    kind: 'invalid',
    httpStatus: 422,
  },
  {
    name: 'the token bucket said wait',
    arrange: () =>
      respond('{"code":"rate_limited","message":"slow down"}', {
        status: 429,
        headers: { 'retry-after': '11' },
      }),
    kind: 'rate_limited',
    httpStatus: 429,
  },
  {
    name: 'group resolution did not answer',
    arrange: () =>
      respond('{"code":"unavailable","message":"Group membership could not be checked."}', {
        status: 503,
      }),
    kind: 'tenant_unresolved',
    httpStatus: 503,
  },
  {
    name: 'some other dependency did not answer',
    arrange: () =>
      respond('{"code":"unavailable","message":"The quota broker did not answer."}', {
        status: 503,
      }),
    kind: 'upstream_degraded',
    httpStatus: 503,
  },
  {
    name: 'an uncaught exception in FastAPI',
    arrange: () => respond('{"detail":"Internal Server Error"}', { status: 500 }),
    kind: 'server_error',
    httpStatus: 500,
  },
  {
    name: 'a 2xx whose JSON body was truncated',
    arrange: () => respond('{"pools":[', { status: 200 }),
    kind: 'server_error',
    httpStatus: 200,
  },
]

describe('a failed read is never representable as empty data', () => {
  it.each(FAILURES)('$name -> error, never ok and never empty', async ({ arrange, kind, httpStatus }) => {
    arrange()
    const r = await read('/v1/capacity', never)

    // THE PROPERTY. Not "it is an error" -- that a reader could satisfy by
    // accident. That it is NEITHER of the two statuses which carry, or imply,
    // data about the platform.
    expect(r.status).not.toBe('ok')
    expect(r.status).not.toBe('empty')
    expect(r.status).toBe('error')
    if (r.status !== 'error') throw new Error('unreachable')
    expect(r.error.kind).toBe(kind)
    expect(r.error.httpStatus).toBe(httpStatus)
    // And the message is written for a person, never blank.
    expect(r.error.message.trim()).not.toBe('')
    // No Result member may carry both a failure and rows.
    expect(r).not.toHaveProperty('data')
  })

  it.each(FAILURES)('$name with data in hand -> stale, keeping the old rows', async ({ arrange, kind }) => {
    arrange()
    const r = await read<{ pools: string[] }>('/v1/capacity', never, { previous: PREVIOUS })

    // The stale rule: a refresh that fails while data is on screen must not
    // blank the screen, and must not silently succeed either.
    expect(r.status).toBe('stale')
    if (r.status !== 'stale') throw new Error('unreachable')
    expect(r.data.pools).toEqual(['kept'])
    expect(r.fetchedAt).toBe(PREVIOUS.fetchedAt)
    expect(r.error.kind).toBe(kind)
  })
})

describe('the unrecognised cases are not guessed at', () => {
  it('an unrecognised 403 is not forced into wrong_domain or tenant_disabled', async () => {
    respond('{"code":"forbidden","message":"Something else entirely."}', { status: 403 })
    const r = await read('/v1/capacity', never)
    if (r.status !== 'error') throw new Error('expected an error')
    expect(r.error.kind).toBe('admin_required')
  })

  it('a bare FastAPI 500 gets a message and a NULL code, never an invented one', async () => {
    respond('{"detail":"Internal Server Error"}', { status: 500 })
    const r = await read('/v1/capacity', never)
    if (r.status !== 'error') throw new Error('expected an error')
    // `code` null is meaningful: it says the server produced no envelope.
    expect(r.error.code).toBeNull()
    expect(r.error.message).toContain('Internal Server Error')
  })

  it('an error body that is not JSON does not change the diagnosis', async () => {
    respond('<html>502 Bad Gateway</html>', { status: 502, contentType: 'text/html' })
    const r = await read('/v1/capacity', never)
    if (r.status !== 'error') throw new Error('expected an error')
    expect(r.error.kind).toBe('server_error')
    expect(r.error.httpStatus).toBe(502)
  })

  it('Retry-After is carried through so the UI can say how long', async () => {
    respond('{"message":"slow down"}', { status: 429, headers: { 'retry-after': '11' } })
    const r = await read('/v1/capacity', never)
    if (r.status !== 'error') throw new Error('expected an error')
    expect(r.error.retryAfterSeconds).toBe(11)
  })

  it('a nonsense Retry-After is dropped rather than rendered', async () => {
    respond('{"message":"slow down"}', { status: 429, headers: { 'retry-after': 'tomorrow' } })
    const r = await read('/v1/capacity', never)
    if (r.status !== 'error') throw new Error('expected an error')
    expect(r.error.retryAfterSeconds).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// The successful half, which is the half the spend-field bug lived in
// ---------------------------------------------------------------------------

describe('a successful read', () => {
  it('with rows is ok and carries the data', async () => {
    respond('{"pools":[{"name":"global"}],"generated_at":"2026-09-22T10:00:00Z"}')
    const r = await read<{ pools: unknown[] }>('/v1/capacity', (d) => d.pools.length === 0)
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') throw new Error('unreachable')
    expect(r.data.pools).toHaveLength(1)
    // The age that matters is the SERVER's, not the moment the bytes landed.
    expect(r.serverAt).toBe('2026-09-22T10:00:00Z')
  })

  it('with no rows is empty, and empty carries no data to render', async () => {
    respond('{"pools":[],"generated_at":"2026-09-22T10:00:00Z"}')
    const r = await read<{ pools: unknown[] }>('/v1/capacity', (d) => d.pools.length === 0)
    expect(r.status).toBe('empty')
    // The one structural guarantee: a component that renders rows cannot be
    // reached with zero rows, because there is nowhere to read them from.
    expect(r).not.toHaveProperty('data')
  })

  it('a body with no generated_at leaves serverAt undefined rather than inventing now', async () => {
    respond('{"pools":[{"name":"global"}]}')
    const r = await read<{ pools: unknown[] }>('/v1/capacity', (d) => d.pools.length === 0)
    if (r.status !== 'ok') throw new Error('expected ok')
    expect(r.serverAt).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

describe('a write', () => {
  it('that got the sign-in page back says the change was NOT saved', async () => {
    respond('<!doctype html>', { contentType: 'text/html' })
    const r = await write('/v1/admin/pools/global', 'PUT', { hard_limit: 4 })
    if (r.status !== 'error') throw new Error('expected an error')
    expect(r.error.kind).toBe('session_expired')
    expect(r.error.message).toContain('NOT saved')
  })

  it('never reports stale: there is no previous value for a mutation', async () => {
    respond('{"message":"nope"}', { status: 503 })
    const r = await write('/v1/admin/pools/global', 'PUT', {})
    expect(r.status).toBe('error')
  })

  it('that succeeded with an unreadable echo is still a success', async () => {
    // Reporting an error here would tell somebody their change failed when it
    // did not -- the mirror image of this platform's defining bug.
    respond('{"truncated', { status: 200 })
    const r = await write('/v1/admin/pools/global', 'PUT', {})
    expect(r.status).toBe('ok')
  })

  it('sends a content-type only when there is a body to type', async () => {
    const spy = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response('{}', { headers: { 'content-type': 'application/json' } }),
    )
    globalThis.fetch = spy as unknown as typeof fetch

    const headersOf = (call: number): Record<string, string> => {
      const init = spy.mock.calls[call]?.[1]
      if (!init) throw new Error(`fetch was not called ${call + 1} time(s)`)
      return init.headers as Record<string, string>
    }

    await write('/v1/tasks/t1/cancel', 'POST')
    expect(headersOf(0)['content-type']).toBeUndefined()

    await write('/v1/tasks', 'POST', { prompt: 'x' })
    expect(headersOf(1)['content-type']).toBe('application/json')
    expect(spy.mock.calls[1]?.[1]?.body).toBe('{"prompt":"x"}')
  })
})

// ---------------------------------------------------------------------------
// The probe registry, which is the screen's self-report
// ---------------------------------------------------------------------------

describe('the probe registry', () => {
  it('does not let a later failure erase the age of the last good payload', async () => {
    const path = '/v1/probe-age'
    respond('{"pools":[{"name":"global"}]}')
    await read<{ pools: unknown[] }>(path, (d) => d.pools.length === 0)

    const afterSuccess = probeSnapshot().find((p) => p.path === path)
    expect(afterSuccess?.lastSuccessAt).not.toBeNull()

    respond('{"message":"gone"}', { status: 503 })
    await read(path, never)

    const afterFailure = probeSnapshot().find((p) => p.path === path)
    // THE POINT OF THE STRIP. A panel showing a four-minute-old number while
    // its route has been 503ing for three of them looks identical to a healthy
    // panel unless the age of the last SUCCESS survives the failure.
    expect(afterFailure?.lastSuccessAt).toBe(afterSuccess?.lastSuccessAt)
    expect(afterFailure?.lastStatus).toBe(503)
    expect(afterFailure?.lastKind).toBe('upstream_degraded')
  })

  it('returns the same array identity until something changes', async () => {
    // useSyncExternalStore compares snapshots BY IDENTITY. A getSnapshot that
    // rebuilds every call is React error #185 -- "Maximum update depth
    // exceeded" -- which is what the first load of this UI actually did.
    const first = probeSnapshot()
    expect(probeSnapshot()).toBe(first)

    respond('{"pools":[{"name":"x"}]}')
    await read<{ pools: unknown[] }>('/v1/probe-identity', (d) => d.pools.length === 0)
    expect(probeSnapshot()).not.toBe(first)
  })
})

// ---------------------------------------------------------------------------
// The two one-liners that carry a disproportionate amount of the honesty
// ---------------------------------------------------------------------------

describe('isPaused compares explicitly and never coerces', () => {
  // CLAUDE.md records `false // true` in jq as a bug that has already cost
  // this repository a working check: a PAUSED pool reported as open. The
  // TypeScript equivalents are `enabled ?? true` and `!enabled`, and both make
  // "the key is missing" and "the value is false" indistinguishable.
  it.each([
    [{ enabled: false }, true],
    [{ enabled: true }, false],
    [{}, false],
    [{ enabled: undefined }, false],
    [{ enabled: null }, false],
    [{ enabled: 0 }, false],
    [{ enabled: '' }, false],
  ] as ReadonlyArray<[{ enabled?: unknown }, boolean]>)('%o -> %s', (pool, expected) => {
    expect(isPaused(pool)).toBe(expected)
  })
})

describe('num keeps a missing number and a zero apart', () => {
  it.each([
    [0, '0'],
    [7, '7'],
    [-1, '-1'],
    [null, '—'],
    [undefined, '—'],
    [Number.NaN, '—'],
    [Number.POSITIVE_INFINITY, '—'],
  ] as ReadonlyArray<[number | null | undefined, string]>)('%s -> %s', (given, expected) => {
    expect(num(given)).toBe(expected)
  })
})

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

const ALL_KINDS: readonly ApiErrorKind[] = [
  'session_expired', 'unauthenticated', 'admin_required', 'wrong_domain',
  'tenant_disabled', 'not_found', 'conflict', 'invalid', 'rate_limited',
  'tenant_unresolved', 'upstream_degraded', 'server_error', 'unreachable',
]

describe('every failure kind has copy', () => {
  it.each(ALL_KINDS)('%s has a heading and a reassurance', (kind) => {
    const e = { kind, httpStatus: null, code: null, message: 'm' }
    expect(errorHeading(e).trim()).not.toBe('')
    expect(errorReassurance(e).trim()).not.toBe('')
  })

  it('the reassurance says a read failed, not that the platform is idle', () => {
    const e = { kind: 'server_error' as const, httpStatus: 500, code: null, message: 'm' }
    expect(errorReassurance(e)).toContain('says nothing about what is running')
  })

  it('a 404 does not promise which of the two reasons it was', () => {
    // store.py returns the same 404 whether the task does not exist or belongs
    // to another tenant, so the copy must not claim to know.
    const e = { kind: 'not_found' as const, httpStatus: 404, code: null, message: 'm' }
    expect(errorReassurance(e)).toContain('belongs to another tenant')
  })
})

// A compile-time assertion with a runtime witness: `Result` must have no
// member that is both a failure and an array. If someone adds
// `{ status: 'error'; data: T }` this stops compiling, which `tsc -b` catches
// in `npm run typecheck` and `make lint`.
type Failures = Extract<Result<{ rows: number[] }>, { status: 'error' | 'empty' }>
type HasData = Failures extends { data: unknown } ? true : false
const noFailureCarriesData: HasData = false

describe('the Result type itself', () => {
  it('has no member that is both a failure and data', () => {
    expect(noFailureCarriesData).toBe(false)
  })
})
