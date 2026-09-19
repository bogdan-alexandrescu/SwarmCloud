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
  if (status === 503) return { ...base, kind: 'upstream_degraded' }
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
    case 'upstream_degraded': return 'We could not confirm which team you belong to'
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
    return asStale(classify(res.status, env, Number.isFinite(ra) && ra > 0 ? ra : undefined))
  }

  let data: T
  try {
    data = (await res.json()) as T
  } catch (err) {
    // 2xx, content-type said JSON, body was not. Truncation or a proxy.
    return asStale({
      kind: 'server_error',
      httpStatus: res.status,
      code: null,
      message: err instanceof Error ? err.message : 'The response body was not readable.',
    })
  }

  const fetchedAt = Date.now()
  // `generated_at` when the body carries one: the age that matters is the
  // server's, not the moment the bytes arrived here.
  const serverAt =
    isRecord(data) && typeof data.generated_at === 'string' ? data.generated_at : undefined

  if (isEmpty(data)) return { status: 'empty', fetchedAt, serverAt }
  return { status: 'ok', data, fetchedAt, serverAt }
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
