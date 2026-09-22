import { useEffect, useState, useSyncExternalStore } from 'react'
import { probeSnapshot, subscribeProbes, type ProbeRecord } from './fetch'
import { timeAgo } from './Shell'

/**
 * The data-source strip: the screen's own self-report.
 *
 * This is what an operator looks at when a number seems wrong. One cell per
 * route, each carrying the status of the most recent attempt, how long it
 * took, and — the part that matters — the age of the newest SUCCESSFUL
 * payload. A panel showing a figure from four minutes ago while its route has
 * been failing for three of them is indistinguishable from a healthy panel
 * unless something says so.
 *
 * A 403 here is information, not a failure. A non-admin genuinely cannot read
 * /v1/admin/*, and the strip saying so is what stops the page looking broken.
 * A 401 is different: that is an expired session and carries a re-auth action.
 *
 * WHY IT IS COLLAPSED NOW. Sixteen cells of it sat at the foot of EVERY route,
 * identical on all of them, spending roughly 200px of a screen whose whole
 * claim is that it fits. It is genuinely useful and is not deleted: it collapses
 * to ONE line carrying the two facts worth glancing at — how many routes this
 * tab has called and the p95 of their latest reads — and opens on click to the
 * full cells. A failure or an expired session is never behind the disclosure:
 * those open the strip by themselves and the re-auth button sits outside it
 * entirely, because a control that appears only after a click is a control that
 * is not there.
 *
 * `<details>` rather than component state on purpose. The element keeps its own
 * open/closed state in the DOM, so the strip does not snap shut every time a
 * poll lands — which, on a screen that re-renders on a 20-second timer, is the
 * difference between a disclosure and a flicker.
 */
export function DataSourceStrip() {
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  const [, tick] = useState(0)

  // The ages shown here are the whole point, so they have to move on their own
  // rather than only when a fetch happens to land.
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  if (probes.length === 0) return null

  const expired = probes.some((p) => p.lastKind === 'unauthenticated' || p.lastKind === 'session_expired')
  // An admin gate is NOT a fault: a non-admin genuinely cannot read
  // /v1/admin/*, and counting it here is what would make a working page report
  // itself broken on every screen.
  const faults = probes.filter((p) => p.lastKind !== null && p.lastKind !== 'admin_required').length
  const gated = probes.filter((p) => p.lastKind === 'admin_required').length
  const p95 = percentile(probes.map((p) => p.lastLatencyMs), 95)

  return (
    <footer className="sources" aria-label="Data sources">
      {expired && (
        <button className="reauth" onClick={() => window.location.reload()}>
          Session expired — reload to sign in
        </button>
      )}
      {/* Open by default only when something is wrong. A clean strip is one
          line; a strip with a fault in it is already showing you which. */}
      <details className="sources-detail" open={faults > 0}>
        <summary className={`sources-line${faults > 0 ? ' is-bad' : ''}`}>
          <span className="sl-count">
            {probes.length} route{probes.length === 1 ? '' : 's'} read
          </span>
          {/* p95 OF WHAT, said in the title rather than left to be guessed: it
              is the 95th percentile across routes of each route's LATEST read,
              not across every read this tab has made. The registry keeps one
              record per path, so the second figure does not exist to report. */}
          <span
            className="sl-p95"
            title={`95th percentile of the latest read of each of the ${probes.length} routes this tab has called. Not a percentile over every request: the registry keeps one record per path.`}
          >
            p95 {p95}ms
          </span>
          {faults > 0 ? (
            <span className="sl-bad">
              {faults} failing
            </span>
          ) : (
            <span className="sl-ok">all answering</span>
          )}
          {gated > 0 && (
            <span className="sl-info" title="A 403 on an /v1/admin route is the expected answer for a non-admin, not a fault.">
              {gated} admin-gated
            </span>
          )}
        </summary>

        <div className="source-cells">
          {probes.map((p) => (
            <Cell key={p.path} probe={p} />
          ))}
        </div>
        <p className="sources-more">
          Every route, its outcome and the age of its newest payload:{' '}
          <a href="#reference">API surface →</a>
        </p>
      </details>
    </footer>
  )
}

/**
 * The p95 of the latest read of each route.
 *
 * Nearest-rank, and deliberately not an interpolating percentile: with a dozen
 * samples an interpolated p95 is a number that matches no request anyone made,
 * and this figure is read as "the slowest thing on this page is about this
 * slow". An empty list returns 0 and is unreachable — the caller has already
 * returned for a zero-length registry — but it returns a number rather than
 * throwing, because a strip that crashes hides every other route's status.
 */
export function percentile(values: readonly number[], p: number): number {
  if (values.length === 0) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const rank = Math.ceil((p / 100) * sorted.length)
  const i = Math.min(sorted.length - 1, Math.max(0, rank - 1))
  return sorted[i] ?? 0
}

function Cell({ probe }: { probe: ProbeRecord }) {
  const { tone, label } = describe(probe)
  return (
    <div className={`source ${tone}`} title={detailFor(probe)}>
      <span className="s-path">{probe.path}</span>
      <span className="s-status">{label}</span>
      <span className="s-age">
        {probe.lastSuccessAt === null ? 'never loaded' : timeAgo(probe.lastSuccessAt)}
      </span>
    </div>
  )
}

function describe(p: ProbeRecord): { tone: 'ok' | 'warn' | 'bad' | 'info'; label: string } {
  if (p.lastKind === null) {
    return { tone: 'ok', label: `${p.lastStatus ?? 200} · ${p.lastLatencyMs}ms` }
  }
  switch (p.lastKind) {
    case 'admin_required':
      // Expected for a non-admin, and saying so is the point.
      return { tone: 'info', label: '403 · admin only' }
    case 'unauthenticated':
    case 'session_expired':
      return { tone: 'bad', label: 'session expired' }
    case 'rate_limited':
      return { tone: 'warn', label: '429 · paused' }
    case 'tenant_unresolved':
      // Same status, different thing to do about it, so it gets its own label
      // rather than falling through to the generic one.
      return { tone: 'warn', label: '503 · tenant unresolved' }
    case 'upstream_degraded':
      return { tone: 'warn', label: '503 · degraded' }
    case 'unreachable':
      return { tone: 'bad', label: 'unreachable' }
    default:
      return { tone: 'bad', label: `${p.lastStatus ?? '—'} · ${p.lastKind}` }
  }
}

function detailFor(p: ProbeRecord): string {
  const last =
    p.lastSuccessAt === null
      ? 'This route has never returned a payload in this session.'
      : `Newest successful payload: ${new Date(p.lastSuccessAt).toLocaleTimeString()}.`
  return `${p.path}\nLast attempt ${new Date(p.lastAttemptAt).toLocaleTimeString()}, ${p.lastLatencyMs}ms.\n${last}`
}
