import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { errorHeading, errorReassurance, type ApiError, type Result } from './fetch'
import { timeAgo } from './types'

/**
 * Every screen loads through this, so no screen can forget a state.
 *
 * `children` only ever receives DATA. There is no way to reach it with a
 * failure and no way to reach it with zero rows, which is the property the
 * whole app is built on -- a 403 cannot reach a component that renders rows,
 * and neither can an empty list pretending to be one.
 *
 * This component also OWNS the stale rule. A screen's `load` does not have to
 * remember the last good data: if a refresh fails while data is on screen,
 * this converts the failure into `stale` and keeps the rows visible, dimmed,
 * with the age and the error in the header. Putting that in one place is why
 * it cannot be forgotten per-screen.
 */
export function Screen<T>({
  title,
  load,
  summary,
  empty,
  children,
}: {
  title: string
  load: () => Promise<Result<T>>
  /** One line under the title once data is in. */
  summary?: (data: T) => ReactNode
  /** Shown when the read SUCCEEDED and returned nothing. Different from failure. */
  empty?: { heading: string; body: ReactNode }
  children: (data: T) => ReactNode
}) {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  /** The last read that produced rows. Survives a failed refresh on purpose. */
  const lastGood = useRef<{ data: T; fetchedAt: number } | null>(null)
  /** Set by a 429 so the retry control can say how long, rather than lying. */
  const [pausedUntil, setPausedUntil] = useState<number | null>(null)

  useEffect(() => {
    let live = true
    // A refresh with data on screen must NOT blank it back to skeletons.
    if (!lastGood.current) setState({ status: 'loading', since: Date.now() })

    load().then((next) => {
      if (!live) return

      if (next.status === 'ok') {
        lastGood.current = { data: next.data, fetchedAt: next.fetchedAt }
        setPausedUntil(null)
        setState(next)
        return
      }
      if (next.status === 'empty') {
        // A genuine zero replaces the old rows. Keeping them would be the
        // mirror of the bug this app is about: showing data that is gone.
        lastGood.current = null
        setPausedUntil(null)
        setState(next)
        return
      }
      if (next.status === 'error') {
        const prev = lastGood.current
        if (next.error.kind === 'rate_limited' && next.error.retryAfterSeconds) {
          setPausedUntil(Date.now() + next.error.retryAfterSeconds * 1000)
        }
        // THE STALE RULE. Data in hand plus a failed refresh is never a blank
        // screen -- it is the old data, dimmed, labelled with its age.
        setState(
          prev
            ? { status: 'stale', data: prev.data, fetchedAt: prev.fetchedAt, error: next.error }
            : next,
        )
        return
      }
      setState(next)
    })
    return () => {
      live = false
    }
    // `load` is recreated per render by callers; nonce is the retry trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce])

  const retry = useCallback(() => setNonce((n) => n + 1), [])

  const data =
    state.status === 'ok' ? state.data : state.status === 'stale' ? state.data : null

  return (
    <>
      {/* NO ENVIRONMENT BADGE HERE ANY MORE. Every screen used to print a
          hardcoded `dev` beside its own title -- a word nothing in this app
          had measured, repeated on sixteen screens. Overview.tsx had already
          refused to draw it and said why; Brand.tsx now draws the real one
          once, in the product header, from something that was actually
          established. A per-screen copy would be a second opinion about the
          environment, and the second opinion is the one that gets believed
          because it is next to what you are reading. */}
      <div className="head">
        <h1>{title}</h1>
      </div>

      <p className="sub">
        <SubLine state={state} summary={summary} pausedUntil={pausedUntil} onRetry={retry} />
      </p>

      {state.status === 'stale' && <StaleBanner error={state.error} fetchedAt={state.fetchedAt} />}

      {state.status === 'loading' && <SkeletonRows />}
      {state.status === 'error' && <FailedPanel error={state.error} onRetry={retry} />}

      {state.status === 'empty' && empty && (
        <div className="state">
          <h3>{empty.heading}</h3>
          <p>{empty.body}</p>
          <p className="checked-at">Checked {timeAgo(state.fetchedAt)}.</p>
        </div>
      )}

      {/* Dimmed when stale, and the dimming is the signal that the numbers
          below are from an earlier read. */}
      {data !== null && (
        <div className={state.status === 'stale' ? 'stale-body' : undefined}>{children(data)}</div>
      )}
    </>
  )
}

function SubLine<T>({
  state,
  summary,
  pausedUntil,
  onRetry,
}: {
  state: Result<T>
  summary?: (data: T) => ReactNode
  pausedUntil: number | null
  onRetry: () => void
}) {
  const paused = pausedUntil !== null && pausedUntil > Date.now()
  const retryBtn = (
    <button onClick={onRetry} disabled={paused}>
      {paused ? `paused ${Math.ceil((pausedUntil - Date.now()) / 1000)}s` : 'refresh'}
    </button>
  )

  switch (state.status) {
    case 'loading':
      return <>Reading…</>
    case 'ok':
      return (
        <>
          {summary?.(state.data)} · read {timeAgo(state.serverAt ?? state.fetchedAt)} {retryBtn}
        </>
      )
    case 'empty':
      return <>Nothing to show · read {timeAgo(state.serverAt ?? state.fetchedAt)} {retryBtn}</>
    case 'stale':
      return (
        <>
          {summary?.(state.data)} · <strong>not refreshed</strong> · showing{' '}
          {timeAgo(state.fetchedAt)} {retryBtn}
        </>
      )
    case 'error':
      return state.error.kind === 'admin_required' ? (
        <>Admin only.</>
      ) : (
        <>Could not read. {retryBtn}</>
      )
  }
}

/**
 * Shown above data that is real but no longer current. Deliberately not a
 * toast: at 390pt a toast sits under the thumb and gets dismissed by accident,
 * and the one thing this must guarantee is that the failure is still on screen
 * when the operator looks.
 */
function StaleBanner({ error, fetchedAt }: { error: ApiError; fetchedAt: number }) {
  return (
    <div className="state stale-note" role="status">
      <h3>{errorHeading(error)} — showing older data</h3>
      <p>
        The numbers below were read {timeAgo(fetchedAt)} and have not been refreshed
        since. {error.message}
      </p>
    </div>
  )
}

/**
 * The panel that exists because this platform's defining bug was rendering a
 * failed read as an empty one. The second paragraph is not decoration: without
 * it a reader takes an error screen as evidence about the platform.
 */
export function FailedPanel({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const reload = error.kind === 'session_expired' || error.kind === 'unauthenticated'

  // AN ADMIN GATE IS NOT A FAILURE. A non-admin genuinely cannot read
  // /v1/admin/*, and painting that red -- with "this is a failure to read the
  // platform" under it -- tells someone their platform is broken when they
  // are simply not an admin. The Trouble board got this right panel-by-panel
  // and every screen using this component got it wrong.
  if (error.kind === 'admin_required') {
    return (
      <div className="state admin-gate" role="status">
        <h3>{errorHeading(error)}</h3>
        <p>
          You are not in an admin group, so this screen has nothing to show
          you. Nothing is wrong with the platform, and nothing failed.
        </p>
        <p className="checked-at">{error.message}</p>
      </div>
    )
  }

  return (
    <div className="state failed">
      <h3>{errorHeading(error)}</h3>
      <p>{error.message}</p>
      <p style={{ marginTop: 8 }}>{errorReassurance(error)}</p>
      {error.httpStatus !== null && (
        <p className="checked-at">
          HTTP {error.httpStatus}
          {error.code ? ` · ${error.code}` : ''}
        </p>
      )}
      {typeof error.detail === 'string' && <pre>{error.detail}</pre>}
      {reload ? (
        <button className="retry" onClick={() => window.location.reload()}>
          Reload to sign in
        </button>
      ) : error.kind === 'rate_limited' ? (
        // A COUNTDOWN, NEVER A RETRY BUTTON. The server has just told us how
        // long to wait; offering "Try again" invites someone to hammer the
        // wall they were told about, and the token bucket is 20 rps per
        // principal per instance -- every open tab counts against it.
        <p className="checked-at">
          Retrying is disabled for {error.retryAfterSeconds ?? 'a few'} more
          seconds, because the API asked us to wait that long.
        </p>
      ) : (
        <button className="retry" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function SkeletonRows({ rows = 6 }: { rows?: number }) {
  return (
    <div className="section" aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton" key={i} style={{ height: 38, marginBottom: 6, borderRadius: 8 }} />
      ))}
    </div>
  )
}

/**
 * MOVED to types.ts, and re-exported here rather than left behind as a second
 * copy.
 *
 * `checks.ts` needed it, and that module is pure on purpose -- Results in,
 * `Check[]` out, no React anywhere in its import graph, which is what lets
 * `node --test` exercise it without a DOM. Importing it from this file would
 * have pulled every component in Shell.tsx along with it.
 *
 * The eleven existing `import { timeAgo } from './Shell'` sites are untouched.
 */
export { timeAgo }

/**
 * AN IDENTIFIER, RENDERED SO IT CAN BE PASTED. B17.
 *
 * `styles.css:178` uppercases `.section > h2`, and two screens put an id in
 * one: `Workflows.tsx` prints the workflow id there and `Agents.tsx` prints it
 * again as a group heading. The result on screen is
 * `WF_BCDC9180E4FB4A209F31` for an id that is lowercase in Firestore, in the
 * API, in every log line and in the URL you would paste it into. A displayed
 * id that differs from the real one is not a cosmetic problem: it is unusable
 * for the one thing an id is for. `QuotaDetail.tsx:94-96` already refuses to
 * do this to tenant ids and writes the reason beside the refusal; this is that
 * refusal made general.
 *
 * IT IS A COMPONENT AND A CLASS, NOT A CONVENTION. `.id` in styles.css sets
 * `text-transform: none` on the element ITSELF, so it wins over any ancestor's
 * transform by inheritance rather than by out-specifying it -- which means a
 * heading, chip or table cell added later cannot break it from above. Wrapping
 * the value is the only thing a caller has to remember, and
 * `src/__tests__/brand.test.tsx` asserts the computed style rather than the
 * source text, so deleting the CSS rule fails the suite.
 */
export function Id({
  children,
  title,
}: {
  children: ReactNode
  title?: string
}) {
  return (
    <span className="id" title={title}>
      {children}
    </span>
  )
}

export function Nav({ at, go }: { at: string; go: (to: string) => void }) {
  const tabs = [
    ['home', 'Home'],
    ['trouble', 'Trouble'],
    ['capacity', 'Capacity'],
    ['holders', 'Holders'],
    ['agents', 'Agents'],
    ['workflows', 'Workflows'],
    ['activity', 'Activity'],
    ['quota', 'Quota'],
    ['counts', 'Counts'],
    ['tenants', 'Tenants'],
    ['settings', 'Settings'],
  ] as const
  return (
    <nav className="nav">
      {tabs.map(([id, label]) => (
        <button key={id} className={at === id ? 'on' : ''} onClick={() => go(id)}>
          {label}
        </button>
      ))}
    </nav>
  )
}
