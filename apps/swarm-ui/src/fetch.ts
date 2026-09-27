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
// Routes (CH-18)
// ---------------------------------------------------------------------------
// A ROUTE IS A PATH TEMPLATE WITH THE IDS AND THE QUERY REMOVED, and it is
// what the probe registry below is keyed by. It was keyed by the concrete URL,
// so every task anyone opened added a "route" of its own --
// `/v1/tasks/<id>/attempts?limit=50` was 13 of the 29 cells on the dock the QA
// pass screenshotted -- which inflated the route count, shaped the p95 and
// made the help text's "one record per route" untrue.
//
// `read`, `write` and `noteFixtureProbe` take ONLY the value `route()` builds,
// never a string, so the typecheck refuses any call that would key a probe by
// a concrete URL. A hand-kept list of call sites could not do that: the first
// draft of the decision's list missed four. The value is branded so it cannot
// be forged as an object literal either.
//
// The template keeps its `{placeholders}` and any literal query: the seam
// tests (`test_runtimes_screen.py`, `test_account_api.py`,
// `test_checkpoint_content.py`) read the path literals out of the source and
// compare their SHAPE with the routers' declarations, `{id}` and all.

declare const ROUTE: unique symbol

/** A read or write target: the URL to request, and the route it belongs to. */
export interface ApiRoute {
  /** The concrete URL, ids substituted and the query appended. */
  readonly url: string
  /** The registry key: the template with any `?…` removed. */
  readonly template: string
  readonly [ROUTE]: true
}

/**
 * A path segment the caller has ALREADY encoded. The one user is a checkpoint
 * member path, whose `/` separators the route's `{path:path}` expects to
 * arrive as separators (`memberHref` in CheckpointBrowser.tsx encodes each
 * segment and refuses `.` and `..`).
 */
export interface Encoded {
  readonly encoded: string
}

/** Mark a value as already encoded, so `route()` substitutes it verbatim. */
export function encoded(value: string): Encoded {
  return { encoded: value }
}

/**
 * Build a route: `{name}` in `template` becomes `encodeURIComponent(params.name)`
 * (or the value verbatim, for an `encoded()` one), `query` is appended, and
 * the registry key is the template with any literal query removed.
 *
 * A placeholder with no value THROWS rather than requesting a URL with a
 * literal `{id}` in it, which the API would answer with a 404 that reads as
 * "this task does not exist".
 */
export function route(
  template: string,
  params: Readonly<Record<string, string | number | Encoded>> = {},
  query?: string | URLSearchParams,
): ApiRoute {
  const url = template.replace(/\{([A-Za-z_][\w]*)\}/g, (_m, name: string) => {
    const v = params[name]
    if (v === undefined) throw new Error(`route ${template} has no value for {${name}}`)
    return typeof v === 'object' ? v.encoded : encodeURIComponent(String(v))
  })
  const q = query === undefined ? '' : String(query)
  const full = q === '' ? url : `${url}${url.includes('?') ? '&' : '?'}${q}`
  const at = template.indexOf('?')
  return { url: full, template: at === -1 ? template : template.slice(0, at) } as ApiRoute
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
//
// ONE RECORD PER ROUTE (CH-18). Its status is the last attempt of ANY call to
// the route and its age the newest successful payload of any call to it; each
// panel still carries its own age and its own failure, and `lastUrl` says
// which concrete call the status belongs to.

export interface ProbeRecord {
  /** The route: a path template, ids and query removed. */
  path: string
  /** The concrete URL of the last attempt, so a failure can be traced to its task. */
  lastUrl: string
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
  target: ApiRoute,
  latencyMs: number,
  ok: boolean,
  // Which failure to simulate. Defaulted to a 403 because that is the cell
  // whose treatment is easiest to get wrong -- it must read as information,
  // not breakage -- and a fixture that always produced a 503 meant that path
  // was never once looked at in development.
  kind: ApiErrorKind = 'admin_required',
  options: { frame?: boolean } = {},
): void {
  const status = ok ? 200 : kind === 'admin_required' ? 403 : 503
  recordProbe({
    path: target.template,
    lastUrl: target.url,
    lastStatus: status,
    lastKind: ok ? null : kind,
    lastLatencyMs: latencyMs,
    lastAttemptAt: Date.now(),
    lastSuccessAt: ok ? Date.now() : null,
  })
  // A fixture read registers when it LANDS, after its simulated latency, so it
  // belongs to the screen that was open when it STARTED -- `latencyMs` ago.
  // Which of an inspector and the page under it asked is not knowable by
  // then, so it is the inspector's when one was open (development only: the
  // live path below knows).
  if (options.frame !== true) {
    const scope = shownBy(frameAt(Date.now() - latencyMs))
    scope.settled += 1
    if (ok) scope.newestSuccessAt = Date.now()
    else if (kind !== 'admin_required') scope.failed += 1
    publishScreenReads()
  }
}

// ---------------------------------------------------------------------------
// The screen's own reads (CH-2)
// ---------------------------------------------------------------------------
// THE HEAD'S AGE IS THE SCREEN'S, AND THE DOCK'S IS THE TAB'S. The head used
// to show the newest success of ANY route this tab had called, so it said
// "just now" beside a page that was still loading -- the frame's own identity
// read had landed, the page's had not. The dock keeps the tab-wide view (its
// label says "every route"); the head reads only what the current screen
// asked for.
//
// A SCREEN IS A ROUTE OF THE APP (`canonical()` in App.tsx), begun when the
// route changes. A read belongs to the screen that was open when it STARTED,
// so a slow read from the screen you just left cannot land as "newest read
// just now" on the one you opened. The frame's own reads -- the identity read
// behind the product header -- pass `frame: true` and belong to no screen.
//
// A ROUTE CAN SHOW TWO SCREENS: the agent inspector over the Agents list. The
// list is never unmounted while the inspector is open, so opening, switching
// and closing the inspector change the route and leave the list exactly as it
// was -- it re-reads only on its next poll. So a route has a PAGE, the screen
// the rail points at, and at most one INSPECTOR over it:
//
//   - the page's reads carry on across the inspector: a route whose page is
//     the page already open keeps that page's reads, rather than starting from
//     nothing beside a list that is fully drawn and asking for nothing. That
//     was the defect -- close the drawer and the head said "reading…" with no
//     read in flight, for up to 30s, or for good once polling had stopped;
//   - the inspector's reads are its own, and start from nothing whenever the
//     route changes (each pane is its own `Screen` and reads on mount);
//   - a read the page asked for is the PAGE's even while an inspector is open,
//     so the list's polls neither pass for the inspector's reads nor go
//     missing from the list's own age. `Screen` says which it is by calling
//     its load inside `pageReads`; anything else is the inspector's when one
//     is open, and the page's when not.
//
// A screen re-entered through another page is still a screen re-read: only
// the page open at the moment of the route change carries on.

export interface ScreenReads {
  /** The route these are about, or null before any screen has begun. */
  key: string | null
  /** Reads started and not yet settled. */
  inFlight: number
  /** Reads that settled, one way or the other. */
  settled: number
  /** Settled reads that failed, the admin gate excepted. */
  failed: number
  /** The newest successful payload any of this screen's reads produced. */
  newestSuccessAt: number | null
}

/** One screen's reads. `page` names the page it is, for an inspector null. */
type Scope = Omit<ScreenReads, 'key'> & { page: string | null }

/** One route: its page's reads, and the inspector's over it if one is open. */
interface Frame {
  key: string | null
  startedAt: number
  page: Scope
  inspector: Scope | null
}

const EMPTY_SCOPE: ScreenReads = { key: null, inFlight: 0, settled: 0, failed: 0, newestSuccessAt: null }

function fresh(page: string | null): Scope {
  return { page, inFlight: 0, settled: 0, failed: 0, newestSuccessAt: null }
}

/** Newest last. Kept short: only a read's START is ever looked up in it. */
let frames: Frame[] = []
let screenSnapshot: ScreenReads = EMPTY_SCOPE
const screenListeners = new Set<() => void>()
/** Above zero while `pageReads` is running a page's load. */
let pageDepth = 0

function currentFrame(): Frame {
  const last = frames[frames.length - 1]
  if (last !== undefined) return last
  // Before any screen has begun (a read from a module-level load, a test that
  // renders a component alone): an unnamed frame, which no head ever shows.
  const frame: Frame = { key: null, startedAt: 0, page: fresh(null), inspector: null }
  frames.push(frame)
  return frame
}

/** The route that was open at `t`. */
function frameAt(t: number): Frame {
  for (let i = frames.length - 1; i >= 0; i--) {
    const f = frames[i]!
    if (f.startedAt <= t) return f
  }
  return frames[0] ?? currentFrame()
}

/** The screen the head speaks for on a route: the inspector, if one is open. */
function shownBy(frame: Frame): Scope {
  return frame.inspector ?? frame.page
}

function publishScreenReads(): void {
  const f = currentFrame()
  const s = shownBy(f)
  screenSnapshot = {
    key: f.key,
    inFlight: s.inFlight,
    settled: s.settled,
    failed: s.failed,
    newestSuccessAt: s.newestSuccessAt,
  }
  for (const fn of screenListeners) fn()
}

/**
 * A route is open. `page` is the page under an inspector -- the route with its
 * task removed -- and is omitted when the route is a page by itself.
 *
 * A page's reads start from nothing -- a screen re-entered is a screen
 * re-read, and the age of its last visit is not the age of what it is about to
 * draw -- UNLESS it is the page already open: then it never unmounted, and its
 * reads carry on (above). An inspector's reads always start from nothing.
 */
export function beginScreenReads(key: string, page: string | null = null): void {
  const pageKey = page ?? key
  const last = frames[frames.length - 1]
  frames.push({
    key,
    startedAt: Date.now(),
    page: last !== undefined && last.page.page === pageKey ? last.page : fresh(pageKey),
    inspector: page === null ? null : fresh(null),
  })
  if (frames.length > 8) frames = frames.slice(-8)
  publishScreenReads()
}

/**
 * Run a PAGE's load: every read it starts before its first `await` is the
 * page's, even while an inspector is open over it. `Screen` calls its load
 * through this when it is the page (`RoutedPage` in Shell.tsx). A read started
 * after that first `await` is outside it, and follows the route's own rule.
 */
export function pageReads<T>(load: () => T): T {
  pageDepth += 1
  try {
    return load()
  } finally {
    pageDepth -= 1
  }
}

export function subscribeScreenReads(fn: () => void): () => void {
  screenListeners.add(fn)
  return () => screenListeners.delete(fn)
}

/** Identity-stable between changes, for `useSyncExternalStore`. */
export function screenReadsSnapshot(): ScreenReads {
  return screenSnapshot
}

/** A live read has started, on behalf of the screen open now (or the frame). */
function startScreenRead(frame: boolean): (outcome: 'ok' | 'failed' | 'admin') => void {
  if (frame) return () => {}
  const route = currentFrame()
  const scope = pageDepth > 0 ? route.page : shownBy(route)
  scope.inFlight += 1
  publishScreenReads()
  return (outcome) => {
    scope.inFlight -= 1
    scope.settled += 1
    if (outcome === 'ok') scope.newestSuccessAt = Date.now()
    else if (outcome === 'failed') scope.failed += 1
    publishScreenReads()
  }
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

/**
 * Empty the registry, so "nothing has loaded in this tab" is true again.
 *
 * WHY THIS EXISTS. `probes` is module state, and module state in a test file
 * outlives the test that wrote it: Vitest isolates per FILE, not per test. So
 * one test that renders `<App />` and lets a fixture read land leaves a
 * `lastSuccessAt` behind, and the NEXT test in that file sees a head that says
 * "newest read just now" over a render in which nothing has read anything.
 *
 * That is not hypothetical. `shell.test.tsx`'s B3 head assertion -- "a head
 * that rendered a zero age against no successful read would be the defining
 * bug of this product, in the frame" -- failed for exactly this reason on
 * every run of that file on its own, and passed in the full suite only because
 * the tests before it happened to finish before the 300ms fixture landed. A
 * guard that holds only when it runs first is the failure mode this suite was
 * built to remove, so `setup.ts` calls this between tests the same way
 * `restoreMocks` puts a stubbed global back.
 *
 * Nothing in the running application calls it. The app has one registry for
 * the life of the tab, which is the thing the head's age is an age OF.
 */
/*
 * TWO LANES WROTE THIS FUNCTION INDEPENDENTLY and git merged both copies
 * without a conflict, because they landed in different parts of the file.
 * TypeScript caught the redeclaration; nothing else would have. The surviving
 * body is the stricter of the two -- it empties `snapshot` as well as marking
 * it stale, so a reader that ignores the flag cannot serve the old array.
 */
export function forgetProbes(): void {
  probes.clear()
  snapshot = []
  snapshotStale = true
  for (const fn of probeListeners) fn()
  // The screen's own reads are the same module state for the same reason, and
  // the head's "reading…" is exactly as untestable if one test's landed read
  // survives into the next.
  frames = []
  pageDepth = 0
  screenSnapshot = EMPTY_SCOPE
  for (const fn of screenListeners) fn()
}

export interface FetchOptions {
  /** Previous data, if any. A failure with data in hand becomes `stale`. */
  previous?: { data: unknown; fetchedAt: number; serverAt?: string } | null
  signal?: AbortSignal
  /**
   * A read the FRAME makes, not the screen -- the product header's identity
   * read. It registers in the tab-wide registry like any other, and it is not
   * the screen's, so the head's age never counts it (CH-2).
   */
  frame?: boolean
}

/**
 * Perform one read and classify it completely.
 *
 * `isEmpty` decides whether a 2xx body counts as zero rows. It is a parameter
 * rather than a guess, because "empty" is different per endpoint -- a capacity
 * response with no pools and a task page with no tasks are both objects.
 */
export async function read<T>(
  target: ApiRoute,
  isEmpty: (data: T) => boolean,
  opts: FetchOptions = {},
): Promise<Result<T>> {
  const previous = opts.previous ?? null
  const asStale = (error: ApiError): Result<T> =>
    previous
      ? { status: 'stale', data: previous.data as T, fetchedAt: previous.fetchedAt, error }
      : { status: 'error', error }

  const startedAt = Date.now()
  const settle = startScreenRead(opts.frame === true)
  const note = (
    status: number | null,
    kind: ApiErrorKind | null,
    ok: boolean,
  ): void => {
    recordProbe({
      path: target.template,
      lastUrl: target.url,
      lastStatus: status,
      lastKind: kind,
      lastLatencyMs: Date.now() - startedAt,
      lastAttemptAt: Date.now(),
      lastSuccessAt: ok ? Date.now() : null,
    })
    settle(ok ? 'ok' : kind === 'admin_required' ? 'admin' : 'failed')
  }

  let res: Response
  try {
    res = await fetch(target.url, {
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
  target: ApiRoute,
  method: 'POST' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<Result<unknown>> {
  const startedAt = Date.now()
  // A write registers in the tab-wide registry and not as the screen's read:
  // the head's age is "how old are the figures on this page", and a write
  // draws no figure until the screen reads again.
  const note = (status: number | null, kind: ApiErrorKind | null, ok: boolean): void =>
    recordProbe({
      path: target.template,
      lastUrl: target.url,
      lastStatus: status,
      lastKind: kind,
      lastLatencyMs: Date.now() - startedAt,
      lastAttemptAt: Date.now(),
      lastSuccessAt: ok ? Date.now() : null,
    })

  let res: Response
  try {
    res = await fetch(target.url, {
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
