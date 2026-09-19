import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { errorHeading, errorReassurance, type ApiError, type Result } from './fetch'

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
      <div className="head">
        <h1>{title}</h1>
        <span className="env">dev</span>
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
      return <>Could not read. {retryBtn}</>
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

export function timeAgo(when: Date | string | number): string {
  const t =
    typeof when === 'number'
      ? when
      : typeof when === 'string'
        ? new Date(when).getTime()
        : when.getTime()
  if (!Number.isFinite(t)) return 'at an unknown time'
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

export function Nav({ at, go }: { at: string; go: (to: string) => void }) {
  const tabs = [
    ['trouble', 'Trouble'],
    ['capacity', 'Capacity'],
    ['agents', 'Agents'],
    ['workflows', 'Workflows'],
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
