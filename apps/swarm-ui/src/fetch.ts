// Component F3 -- the fetch contract.
//
// THE ONE RULE THIS FILE EXISTS TO ENFORCE
// ----------------------------------------
// A failed request must never be representable as empty data.
//
// That is not a general principle here, it is this platform's defining bug. A
// sweep of its operational scripts confirmed 56 places where a probe failure
// rendered as an absence: `status.sh` printed "no swarm services deployed"
// when a session had expired, and finished on "nothing needs attention". The
// same day a build wrote a manifest listing zero images and reported "ok built
// 6 image(s)". Commit 9c639af is titled "An HTTP error is not an empty
// result". A screen that cannot tell "no agents running" from "the query
// failed" is that bug with a nicer font.
//
// So every value on every screen resolves through `Result<T>`, which has no
// member that is both a failure and an array.

/** The client's classification of a failure. The table is in docs/web-ui/01. */
export type ApiErrorKind =
  | 'session_expired'
  | 'unauthenticated'
  | 'admin_required'
  | 'wrong_domain'
  | 'tenant_disabled'
  | 'not_found'
  | 'conflict'
  | 'invalid'
  | 'rate_limited'
  | 'tenant_unresolved'
  | 'upstream_degraded'
  | 'server_error'
  | 'unreachable'

export interface ApiError {
  kind: ApiErrorKind
  /** null when `fetch` itself rejected, so there was never a response. */
  httpStatus: number | null
  /**
   * The server's own `code`, verbatim from ApiError.to_payload() -- or null.
   * NULL IS MEANINGFUL: an uncaught exception in FastAPI returns
   * `{"detail": "Internal Server Error"}`, a different shape with no code at
   * all, because create_app registers handlers for ApiError, AuthError,
   * InvalidTransition and RequestValidationError and nothing else. Never
   * invent one.
   */
  code: string | null
  /** Written for a human by the server, or by us when there was no envelope. */
  message: string
  detail?: unknown
  /** From `Retry-After` on a 429. Honour it rather than retrying into the wall. */
  retryAfterSeconds?: number
}

/**
 * Five states, never a bare array.
 *
 * `empty` is separate from `ok` on purpose: a component that wants rows must
 * say what it does when there are none, and cannot reach `data` in the case
 * where there are none.
 *
 * `stale` is the one that is easy to leave out and expensive to miss. Any
 * refresh that fails while previous data is in hand produces `stale` -- never
 * `error`, never a silent no-op. The last-known data stays on screen, dimmed,
 * with the error and the age of the data in the header. That is what a
 * dashboard looks like during a Cloud Identity blip or a 429, and it is what
 * stops an operator reading a frozen number as a live one.
 */
export type Result<T> =
  | { status: 'loading'; since: number }
  | { status: 'ok'; data: T; fetchedAt: number; serverAt?: string }
  | { status: 'empty'; fetchedAt: number; serverAt?: string }
  | { status: 'stale'; data: T; fetchedAt: number; error: ApiError }
  | { status: 'error'; error: ApiError }

/** The server's error envelope, errors.py:15-28. `detail` is optional. */
interface ErrorEnvelope {
  code?: unknown
  message?: unknown
  detail?: unknown
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/**
 * Classify a response whose status is not 2xx.
 *
 * Several kinds are distinguished by the MESSAGE rather than the status,
 * because the API returns 403 for three unrelated situations that need three
 * different screens: you are not an admin, your domain is not permitted, and
 * your tenant is disabled. Matching on substrings is fragile, so an
 * unrecognised 403 stays a generic one rather than being forced into a bucket.
 */
function classify(status: number, env: ErrorEnvelope | null, retryAfter: number | undefined): ApiError {
  const code = typeof env?.code === 'string' ? env.code : null
  const message =
    typeof env?.message === 'string' && env.message.trim() !== ''
      ? env.message
      : `The API returned HTTP ${status}.`
  const detail = env?.detail
  const base = { httpStatus: status, code, message, detail }
  const lower = message.toLowerCase()

  if (status === 401) return { ...base, kind: 'unauthenticated' }
  if (status === 403) {
    if (lower.includes('admin group membership is required')) {
      return { ...base, kind: 'admin_required' }
    }
    if (lower.includes('is not permitted')) return { ...base, kind: 'wrong_domain' }
    if (lower.includes('is disabled')) return { ...base, kind: 'tenant_disabled' }
    // An unrecognised 403. Do NOT guess -- guessing picks a gate page that
    // tells the user to fix something that is not wrong.
    return { ...base, kind: 'admin_required' }
  }
  if (status === 404) return { ...base, kind: 'not_found' }
  if (status === 409) return { ...base, kind: 'conflict' }
  if (status === 422) return { ...base, kind: 'invalid' }
  if (status === 429) return { ...base, kind: 'rate_limited', retryAfterSeconds: retryAfter }
  if (status === 503) {
    // A 503 was previously ALWAYS reported as "we could not confirm which team
    // you belong to", because group resolution is this deployment's dominant
    // 503. It is not the only one, and on a deployment with no groups
    // configured at all it describes a failure that cannot happen -- which
    // matters now that SwarmCloud is meant to run outside an organisation,
    // where `tenant_groups` is empty by design and the caller's tenant is
    // always their own.
    //
    // The API says which it is, so the API is asked rather than the status
    // code guessed from.
    if (lower.includes('group membership') || lower.includes('tenant could not be resolved')) {
      return { ...base, kind: 'tenant_unresolved' }
    }
    return { ...base, kind: 'upstream_degraded' }
  }
  return { ...base, kind: 'server_error' }
}

/** Copy for the one thing each failure means, for a person reading the screen. */
export function errorHeading(e: ApiError): string {
  switch (e.kind) {
    case 'session_expired': return 'Your session expired'
    case 'unauthenticated': return 'Your session expired'
    case 'admin_required': return 'This needs admin access'
    case 'wrong_domain': return 'This account cannot use SwarmCloud'
    case 'tenant_disabled': return 'Your tenant is disabled'
    case 'not_found': return 'Not found'
    case 'conflict': return 'That conflicts with what is already registered'
    case 'invalid': return 'That request was not valid'
    case 'rate_limited': return 'Refreshing paused'
    case 'tenant_unresolved': return 'We could not confirm which team you belong to'
    case 'upstream_degraded': return 'A service the API depends on did not answer'
    case 'server_error': return 'The API failed on this request'
    case 'unreachable': return 'SwarmCloud is unreachable'
  }
}

/**
 * The sentence that must appear on every failure surface: this is a failure to
 * READ the platform, and says nothing about what is running. Without it a
 * reader takes an error screen as evidence about the platform -- which is the
 * whole bug, one layer up.
 */
export function errorReassurance(e: ApiError): string {
  if (e.kind === 'not_found') {
    // store.py:369-379 deliberately returns the same 404 whether the task does
    // not exist or belongs to another tenant, so the copy must not promise
    // which one it was.
    return 'This task does not exist, or it belongs to another tenant. The API returns the same answer for both.'
  }
  if (e.kind === 'unreachable') {
    return 'The request never reached the platform, so this says nothing about what is running.'
  }
  return 'This is a failure to read the platform. It says nothing about what is running.'
}

// ---------------------------------------------------------------------------
// The probe registry, for the data-source strip
// ---------------------------------------------------------------------------
// Every read records what happened to it, so the UI can show its own
// self-report: which route, what status, how long it took, and how old the
// newest SUCCESSFUL payload is. That last one is the point -- a panel showing
// a number from four minutes ago while its route has been 500ing for three of
// them looks identical to a healthy panel unless something says otherwise.
//
// A 403 here is information rather than a failure: a non-admin genuinely
// cannot read /v1/admin/*, and saying so stops the page looking broken. A 401
// is different -- that is an expired session and needs a re-auth action.

export interface ProbeRecord {
  path: string
  /** null when fetch itself rejected, so there was never a response. */
  lastStatus: number | null
  lastKind: ApiErrorKind | null
  lastLatencyMs: number
  lastAttemptAt: number
  /** When a read last actually produced a payload. Null if one never has. */
  lastSuccessAt: number | null
}

const probes = new Map<string, ProbeRecord>()
const probeListeners = new Set<() => void>()

// useSyncExternalStore compares snapshots BY IDENTITY. A getSnapshot that
// builds a new array every call therefore looks like a change on every render,
// and React loops until it bails with "Maximum update depth exceeded"
// (minified error #185) -- which is exactly what the first load of this UI did.
//
// So the snapshot is cached and only rebuilt when a probe actually changes.
let snapshot: ProbeRecord[] = []
let snapshotStale = true

function recordProbe(rec: ProbeRecord): void {
  const prev = probes.get(rec.path)
  probes.set(rec.path, {
    ...rec,
    // A failure must NOT erase the age of the last good payload -- that age is
    // exactly what the strip exists to show.
    lastSuccessAt: rec.lastSuccessAt ?? prev?.lastSuccessAt ?? null,
  })
  snapshotStale = true
  for (const fn of probeListeners) fn()
}

/**
 * Let the fixture path register a probe too.
 *
 * Without this the strip only ever appears against the live API, which means
 * it ships having never been looked at -- the same way the headroom rows
 * silently rendered nothing because the fixture's runner_profiles was `{}`.
 * A development mode that exercises fewer components than production is a
 * development mode that hides bugs.
 */
export function noteFixtureProbe(
  path: string,
  latencyMs: number,
  ok: boolean,
  // Which failure to simulate. Defaulted to a 403 because that is the cell
  // whose treatment is easiest to get wrong -- it must read as information,
  // not breakage -- and a fixture that always produced a 503 meant that path
  // was never once looked at in development.
  kind: ApiErrorKind = 'admin_required',
): void {
  const status = ok ? 200 : kind === 'admin_required' ? 403 : 503
  recordProbe({
    path,
    lastStatus: status,
    lastKind: ok ? null : kind,
    lastLatencyMs: latencyMs,
    lastAttemptAt: Date.now(),
    lastSuccessAt: ok ? Date.now() : null,
  })
}

/**
 * Forget every probe, as a fresh tab has none.
 *
 * THE REGISTRY IS MODULE STATE, so inside one test file it outlives the
 * component that filled it. `src/__tests__/shell.test.tsx` asserts that a
 * head with no successful read says "nothing has loaded" and prints no
 * digit -- the defining bug of this product, in the frame -- and that test
 * passed on its own while failing in the file, because an earlier test in
 * the same file had rendered screens whose fixture reads registered
 * successes here.
 *
 * A test that is green alone and red in company is worse than a red one: it
 * is a gate that reports the order tests ran in. So `src/__tests__/setup.ts`
 * calls this after every test, and it is a real function rather than a mock
 * -- there is nothing to mock, the state is a Map in this module.
 */
export function forgetProbes(): void {
  probes.clear()
  snapshotStale = true
  for (const fn of probeListeners) fn()
}

export function subscribeProbes(fn: () => void): () => void {
  probeListeners.add(fn)
  return () => probeListeners.delete(fn)
}

export function probeSnapshot(): ProbeRecord[] {
  if (snapshotStale) {
    snapshot = Array.from(probes.values()).sort((a, b) => a.path.localeCompare(b.path))
    snapshotStale = false
  }
  return snapshot
}

export interface FetchOptions {
  /** Previous data, if any. A failure with data in hand becomes `stale`. */
  previous?: { data: unknown; fetchedAt: number; serverAt?: string } | null
  signal?: AbortSignal
}

/**
 * Perform one read and classify it completely.
 *
 * `isEmpty` decides whether a 2xx body counts as zero rows. It is a parameter
 * rather than a guess, because "empty" is different per endpoint -- a capacity
 * response with no pools and a task page with no tasks are both objects.
 */
export async function read<T>(
  path: string,
  isEmpty: (data: T) => boolean,
  opts: FetchOptions = {},
): Promise<Result<T>> {
  const previous = opts.previous ?? null
  const asStale = (error: ApiError): Result<T> =>
    previous
      ? { status: 'stale', data: previous.data as T, fetchedAt: previous.fetchedAt, error }
      : { status: 'error', error }

  const startedAt = Date.now()
  const note = (
    status: number | null,
    kind: ApiErrorKind | null,
    ok: boolean,
  ): void =>
    recordProbe({
      path,
      lastStatus: status,
      lastKind: kind,
      lastLatencyMs: Date.now() - startedAt,
      lastAttemptAt: Date.now(),
      lastSuccessAt: ok ? Date.now() : null,
    })

  let res: Response
  try {
    res = await fetch(path, {
      headers: { accept: 'application/json' },
      // Behind IAP the browser already holds the session cookie; nothing here
      // handles tokens, and nothing here should.
      credentials: 'same-origin',
      signal: opts.signal,
    })
  } catch (err) {
    // DNS, TLS, offline, CORS preflight, abort. Emphatically NOT "no data",
    // and distinct from every 4xx: this one is never the user's fault.
    note(null, 'unreachable', false)
    return asStale({
      kind: 'unreachable',
      httpStatus: null,
      code: null,
      message: err instanceof Error ? err.message : 'The request did not complete.',
    })
  }

  // THE ROW THAT MATTERS MOST.
  //
  // An expired IAP session does not arrive as a 401. The load balancer answers
  // before swarm-api sees the request, with a 302 to Google's sign-in page,
  // and `fetch` follows redirects by default -- so what lands here is a 200
  // carrying HTML. The naive handler parses it, fails, and paints a calm,
  // healthy, EMPTY platform. That is the status.sh incident reproduced in a
  // browser.
  //
  // So the content-type is checked BEFORE the body, on every response
  // including 2xx.
  const contentType = res.headers.get('content-type') ?? ''
  const isJson = contentType.includes('application/json')

  if (res.ok && !isJson) {
    note(res.status, 'session_expired', false)
    return asStale({
      kind: 'session_expired',
      httpStatus: res.status,
      code: null,
      message:
        'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.',
    })
  }

  if (!res.ok) {
    let env: ErrorEnvelope | null = null
    if (isJson) {
      try {
        const body: unknown = await res.json()
        if (isRecord(body)) {
          // A bare FastAPI 500 is `{"detail": "Internal Server Error"}` -- no
          // `code`, no `message`. Map its `detail` into the message so the
          // panel is not blank, but leave `code` null.
          env =
            typeof body.message === 'string'
              ? (body as ErrorEnvelope)
              : { message: typeof body.detail === 'string' ? body.detail : undefined }
        }
      } catch {
        // A body we cannot read is not a body that changes the diagnosis.
      }
    }
    const ra = Number(res.headers.get('retry-after'))
    const err = classify(res.status, env, Number.isFinite(ra) && ra > 0 ? ra : undefined)
    note(res.status, err.kind, false)
    return asStale(err)
  }

  let data: T
  try {
    data = (await res.json()) as T
  } catch (err) {
    // 2xx, content-type said JSON, body was not. Truncation or a proxy.
    note(res.status, 'server_error', false)
    return asStale({
      kind: 'server_error',
      httpStatus: res.status,
      code: null,
      message: err instanceof Error ? err.message : 'The response body was not readable.',
    })
  }

  note(res.status, null, true)
  const fetchedAt = Date.now()
  // `generated_at` when the body carries one: the age that matters is the
  // server's, not the moment the bytes arrived here.
  const serverAt =
    isRecord(data) && typeof data.generated_at === 'string' ? data.generated_at : undefined

  if (isEmpty(data)) return { status: 'empty', fetchedAt, serverAt }
  return { status: 'ok', data, fetchedAt, serverAt }
}

/**
 * A mutation. Same contract as `read`, same classification, same rules.
 *
 * It shares `classify` and the content-type check deliberately: an expired
 * IAP session arrives as a 200 carrying HTML on a PUT exactly as it does on a
 * GET, and a second copy of that check is how the two would drift apart.
 *
 * There is no `empty` for a write. A mutation that succeeded returns `ok`
 * whether or not the body carried anything, because the question "did this
 * take effect" is answered by the status, not by the payload's length.
 */
export async function write(
  path: string,
  method: 'POST' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<Result<unknown>> {
  const startedAt = Date.now()
  const note = (status: number | null, kind: ApiErrorKind | null, ok: boolean): void =>
    recordProbe({
      path,
      lastStatus: status,
      lastKind: kind,
      lastLatencyMs: Date.now() - startedAt,
      lastAttemptAt: Date.now(),
      lastSuccessAt: ok ? Date.now() : null,
    })

  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: {
        accept: 'application/json',
        ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      },
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    note(null, 'unreachable', false)
    return {
      status: 'error',
      error: {
        kind: 'unreachable',
        httpStatus: null,
        code: null,
        message: err instanceof Error ? err.message : 'The request did not complete.',
      },
    }
  }

  const isJson = (res.headers.get('content-type') ?? '').includes('application/json')

  if (res.ok && !isJson) {
    note(res.status, 'session_expired', false)
    return {
      status: 'error',
      error: {
        kind: 'session_expired',
        httpStatus: res.status,
        code: null,
        message:
          'The API answered with a page instead of data, which is how an expired sign-in arrives. Your change was NOT saved. Reload to sign in again.',
      },
    }
  }

  if (!res.ok) {
    let env: { code?: unknown; message?: unknown; detail?: unknown } | null = null
    if (isJson) {
      try {
        const parsed: unknown = await res.json()
        if (isRecord(parsed)) {
          env =
            typeof parsed.message === 'string'
              ? (parsed as { code?: unknown; message?: unknown; detail?: unknown })
              : { message: typeof parsed.detail === 'string' ? parsed.detail : undefined }
        }
      } catch {
        // A body we cannot read does not change the diagnosis.
      }
    }
    const ra = Number(res.headers.get('retry-after'))
    const error = classify(res.status, env, Number.isFinite(ra) && ra > 0 ? ra : undefined)
    note(res.status, error.kind, false)
    return { status: 'error', error }
  }

  let data: unknown = null
  try {
    data = isJson ? await res.json() : null
  } catch {
    // The write SUCCEEDED; only the echo was unreadable. Reporting an error
    // here would tell someone their change failed when it did not.
    data = null
  }
  note(res.status, null, true)
  return { status: 'ok', data, fetchedAt: Date.now() }
}

/**
 * `enabled` is compared explicitly, never coerced.
 *
 * CLAUDE.md records `false // true` returning `true` in jq as a bug that has
 * already cost this repository a working check -- a PAUSED pool reported as
 * open, which is the one thing that column exists to show. The TypeScript
 * equivalents are `pool.enabled ?? true` and `!pool.enabled` on a possibly
 * absent field: both make "the key is missing" and "the value is false"
 * indistinguishable.
 */
export function isPaused(v: { enabled?: unknown }): boolean {
  return v.enabled === false
}

/**
 * A missing number and a zero are different pixels. Returns an em dash for
 * null/undefined, never "0".
 */
export function num(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : '—'
}
