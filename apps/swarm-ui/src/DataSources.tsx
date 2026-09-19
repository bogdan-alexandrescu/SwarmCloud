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

  return (
    <footer className="sources" aria-label="Data sources">
      {expired && (
        <button className="reauth" onClick={() => window.location.reload()}>
          Session expired — reload to sign in
        </button>
      )}
      <div className="source-cells">
        {probes.map((p) => (
          <Cell key={p.path} probe={p} />
        ))}
      </div>
    </footer>
  )
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
