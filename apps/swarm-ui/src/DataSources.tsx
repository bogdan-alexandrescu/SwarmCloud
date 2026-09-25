import type { ProbeRecord } from './fetch'
import { timeAgo } from './Shell'

/**
 * The data-source cells: the screen's own self-report.
 *
 * This is what an operator looks at when a number seems wrong. One cell per
 * route, each carrying the status of the most recent attempt and -- the part
 * that matters -- the age of the newest SUCCESSFUL payload. A panel showing a
 * figure from four minutes ago while its route has been failing for three of
 * them is indistinguishable from a healthy panel unless something says so.
 *
 * A 403 here is information, not a failure. A non-admin genuinely cannot read
 * /v1/admin/*, and the cell saying so is what stops the page looking broken.
 * A 401 is different: that is an expired session and carries a re-auth action.
 *
 * IT IS NO LONGER A FOOTER (§B18). It rendered ~200px of cards at the bottom
 * of all fifteen routes, identical everywhere, outranking each page's own
 * content for vertical space. The cells are unchanged and are not deleted --
 * they moved into the dock (`Dock.tsx`), behind one line and one click, which
 * is where §B2 puts provenance: "context for everything on screen, not a
 * destination". This component no longer subscribes or ticks; the dock owns
 * both, so the sixteen cells re-render once per tick instead of sixteen times.
 *
 * WHAT CHANGED IN THE OPERATOR PASS. The tone was carried by a left border
 * colour and by the status text's colour -- two channels, both hue, so in the
 * greyscale screenshot that gets pasted into an incident channel a 403 and a
 * dead route were the same cell. Each cell now leads with a `.ctl-dot`, which
 * is the product's shared state vocabulary as a SILHOUETTE: a disc for ok, a
 * flat bar for a fact, a triangle for caution, a diamond for a failure. And a
 * route that has never returned a payload gets `.ctl-em` in the age slot
 * rather than the words "never loaded", because an em dash in the same column
 * as every other age is read as "no age" without anyone having to parse it.
 */
export function DataSourceCells({
  probes,
  now,
}: {
  probes: readonly ProbeRecord[]
  /**
   * The instant every age here is measured against: the dock's, which is the
   * shared clock the head and every screen's sub-line read (CH-1). Left out,
   * each cell reads the wall clock as it renders.
   */
  now?: number | undefined
}) {
  if (probes.length === 0) return null
  return (
    <div className="source-cells">
      {probes.map((p) => (
        <Cell key={p.path} probe={p} now={now} />
      ))}
    </div>
  )
}

function Cell({ probe, now }: { probe: ProbeRecord; now: number | undefined }) {
  const { tone, label } = describe(probe)
  return (
    <div className={`source ${tone}`} title={detailFor(probe)}>
      <span className="s-path">
        <i className={`ctl-dot is-${tone}`} />
        <span className="s-path-t">{probe.path}</span>
      </span>
      <span className="s-status">{label}</span>
      <span className="s-age">
        {probe.lastSuccessAt === null ? (
          <i
            className="ctl-em"
            aria-label={`${probe.path} has never returned a payload in this session, so it has no age. That is an absence, not a fresh read.`}
          >
            —
          </i>
        ) : (
          timeAgo(probe.lastSuccessAt, now)
        )}
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
