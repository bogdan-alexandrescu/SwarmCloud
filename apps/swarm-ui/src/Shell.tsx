import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { Loaded } from './api'

/**
 * Every screen loads through this, so no screen can forget a state.
 *
 * `children` only ever receives DATA. There is no way to reach it with a
 * failure, which is the property the whole app is built on -- a 403 cannot
 * reach a component that renders rows.
 */
export function Screen<T>({
  title,
  load,
  summary,
  empty,
  children,
}: {
  title: string
  load: () => Promise<Loaded<T>>
  /** One line under the title once data is in. */
  summary?: (data: T) => ReactNode
  /** Shown when the read SUCCEEDED and returned nothing. Different from failure. */
  empty?: { heading: string; body: ReactNode }
  children: (data: T) => ReactNode
}) {
  const [state, setState] = useState<Loaded<T>>({ status: 'loading' })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    setState({ status: 'loading' })
    load().then((next) => {
      if (live) setState(next)
    })
    return () => {
      live = false
    }
    // `load` is recreated per render by callers; nonce is the retry trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce])

  const retry = useCallback(() => setNonce((n) => n + 1), [])

  return (
    <>
      <div className="head">
        <h1>{title}</h1>
        <span className="env">dev</span>
      </div>
      <p className="sub">
        {state.status === 'ok' ? (
          <>
            {summary?.(state.data)} · read {timeAgo(state.fetchedAt)}{' '}
            <button onClick={retry}>refresh</button>
          </>
        ) : state.status === 'loading' ? (
          'Reading…'
        ) : (
          'Could not read.'
        )}
      </p>

      {state.status === 'loading' && <SkeletonRows />}
      {state.status === 'failed' && <FailedPanel state={state} onRetry={retry} />}
      {state.status === 'ok' && children(state.data)}
      {state.status === 'ok' && empty && isEmpty(state.data) && (
        <div className="state">
          <h3>{empty.heading}</h3>
          <p>{empty.body}</p>
        </div>
      )}
    </>
  )
}

function isEmpty(data: unknown): boolean {
  if (Array.isArray(data)) return data.length === 0
  if (data && typeof data === 'object') {
    const arrays = Object.values(data).filter(Array.isArray)
    return arrays.length > 0 && arrays.every((a) => a.length === 0)
  }
  return false
}

/**
 * The panel that exists because this platform's defining bug was rendering a
 * failed read as an empty one. The second paragraph is not decoration: without
 * it a reader takes an error screen as evidence about the platform.
 */
export function FailedPanel({
  state,
  onRetry,
}: {
  state: Extract<Loaded<unknown>, { status: 'failed' }>
  onRetry: () => void
}) {
  return (
    <div className="state failed">
      <h3>Could not read this</h3>
      <p>{state.detail}</p>
      <p style={{ marginTop: 8 }}>
        This is a failure to <em>read</em> the platform. It says nothing about what is
        running.
      </p>
      {state.hint && <pre>{state.hint}</pre>}
      <button className="retry" onClick={onRetry}>
        Try again
      </button>
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

export function timeAgo(when: Date | string): string {
  const t = typeof when === 'string' ? new Date(when).getTime() : when.getTime()
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

export function Nav({ at, go }: { at: string; go: (to: string) => void }) {
  const tabs = [
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
