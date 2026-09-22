import { useState } from 'react'
import { loadStats } from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { timeAgo } from './Shell'
import { NEVER_WRITTEN, REAL_STATES, type Stats } from './types'

/**
 * Platform-wide task counts. Admin only, and deliberately behind a button.
 *
 * WHY A BUTTON AND NOT AN AUTO-LOAD. /v1/stats runs one Firestore count()
 * aggregation per TaskState — twelve for a tenant, twenty-four for an admin,
 * because an admin also gets the platform figures. count() bills per 1000
 * index entries scanned, so the cost of this screen grows with the platform's
 * HISTORY, not with how busy it is. It is the one panel that gets more
 * expensive as the platform gets older, and auto-refreshing it at 5s would be
 * a standing charge nobody chose.
 *
 * SCOPE IS NOT DECORATION. `tasks_by_state` is the caller's own tenant;
 * `platform_tasks_by_state` is everyone. They are shown as separate blocks
 * with the scope named, and never side by side in one row — an admin reading
 * their own four running tasks as the platform total is a truth bug, not a
 * layout preference.
 */
export function PlatformCountsScreen() {
  const [run, setRun] = useState<Result<Stats> | null>(null)
  const [runs, setRuns] = useState(0)
  const [busy, setBusy] = useState(false)

  const go = () => {
    setBusy(true)
    loadStats().then((r) => {
      setRun(r)
      setBusy(false)
      // Counted only on a read that actually produced counts. Incrementing on
      // every settled promise would let a failure inflate a number the copy
      // below then calls "aggregations billed".
      if (r.status === 'ok' || r.status === 'stale') setRuns((n) => n + 1)
    })
  }

  const data = run && (run.status === 'ok' || run.status === 'stale') ? run.data : null
  const admin = data ? data.platform_tasks_by_state !== undefined : null

  return (
    <>
      {/* The hardcoded "dev" chip that used to sit here is gone, with the one
          Shell.tsx drew for every other screen. Nothing in this app measured
          that word; Brand.tsx now draws the environment once, in the product
          header, from something that was established. */}
      <div className="head">
        <h1>Platform counts</h1>
      </div>

      <section className="section panel">
        <h2>Run the aggregation</h2>
        <p className="muted">
          This is not loaded automatically. Each run performs one Firestore{' '}
          <code>count()</code> per task state —{' '}
          {admin === null ? 'twelve, or twenty-four as an admin' : admin ? 'twenty-four' : 'twelve'}{' '}
          — and <code>count()</code> bills per 1000 index entries scanned. The
          cost therefore grows with how much history this platform has, not with
          how busy it is right now.
        </p>
        <button className="retry" onClick={go} disabled={busy}>
          {busy ? 'Counting…' : runs === 0 ? 'Run the count' : 'Run it again'}
        </button>
        {runs > 0 && (
          <p className="provenance">
            {runs} successful run{runs === 1 ? '' : 's'} this session
            {data && ` · last read ${timeAgo(data.generated_at)}`}
          </p>
        )}
      </section>

      {run?.status === 'error' && <Failed error={run.error} />}

      {run?.status === 'empty' && (
        <section className="section panel">
          <p className="muted">
            The read succeeded and returned no counts at all. That should be
            impossible — <code>count_tasks_by_state</code> iterates the whole
            enum and writes a key for every state — so treat this as a failed
            query rather than an idle platform.
          </p>
        </section>
      )}

      {data && (
        <>
          <Scope
            title="This tenant"
            scope="tenant"
            subtitle={data.tenant_id}
            counts={data.tasks_by_state}
          />
          {data.platform_tasks_by_state !== undefined ? (
            <Scope
              title="Every tenant"
              scope="platform"
              subtitle="all tenants on this platform"
              counts={data.platform_tasks_by_state}
            />
          ) : (
            <section className="section panel">
              <h2>Every tenant</h2>
              <p className="muted admin-only">
                Admin only. The platform figures are absent from this response
                rather than zero — the API omits the field for a non-admin, and
                an omitted field is not a count of nothing.
              </p>
            </section>
          )}
        </>
      )}
    </>
  )
}

function Failed({ error }: { error: ApiError }) {
  if (error.kind === 'admin_required') {
    return (
      <section className="section panel">
        <p className="muted admin-only">
          Admin only — you are not in an admin group. Nothing failed.
        </p>
      </section>
    )
  }
  return (
    <div className="state failed">
      <h3>{errorHeading(error)}</h3>
      <p>
        {error.code ?? 'error'} — {error.message}
      </p>
      {/* No number anywhere on a failure. A zero here is the most reassuring
          thing on the screen and it would be a guess. */}
      <p style={{ marginTop: 8 }}>
        No counts are shown, because none arrived. This says nothing about how
        much work the platform is carrying.
      </p>
    </div>
  )
}

function Scope({
  title,
  scope,
  subtitle,
  counts,
}: {
  title: string
  scope: 'tenant' | 'platform'
  subtitle: string
  counts: Record<string, number>
}) {
  const real = REAL_STATES.map((s) => ({ state: s, n: counts[s] }))
  const missing = real.filter((r) => typeof r.n !== 'number').map((r) => r.state)
  const max = Math.max(1, ...real.map((r) => (typeof r.n === 'number' ? r.n : 0)))

  // Only summed when every state arrived. A total over a partial response is
  // a wrong number wearing the clothes of a right one.
  const total = missing.length === 0 ? real.reduce((a, r) => a + (r.n as number), 0) : null

  return (
    <section className="section panel">
      <h2>
        {title}
        <span className={`scope ${scope}`}>{subtitle}</span>
      </h2>

      {missing.length > 0 && (
        <p className="warn-text">
          {missing.length} state{missing.length === 1 ? '' : 's'} did not come
          back ({missing.join(', ')}). No total is shown, because a sum over a
          partial response would look like a complete one.
        </p>
      )}

      {real.map(({ state, n }) => (
        <div className="split-row" key={state}>
          <span className="sr-name">{state}</span>
          {/* A STATE THE RESPONSE DID NOT CARRY IS NOT A ZERO (§B20). This
              drew a zero-width fill on a plain track for it, which is the one
              mark the axis now reserves for a MEASURED zero -- so an absent
              count would have been promoted to a measurement by the fix.
              Hatched instead, with no axis, matching the em dash beside it. */}
          <span className={`sr-bar${typeof n === 'number' ? '' : ' is-unknown'}`}>
            <i style={{ width: `${((typeof n === 'number' ? n : 0) / max) * 100}%` }} />
          </span>
          <span className="sr-n">{typeof n === 'number' ? n : '—'}</span>
        </div>
      ))}

      {total !== null && (
        <p className="provenance">{total} task documents across the nine states that are written</p>
      )}

      <p className="muted small">
        {/* Derived from the contract sets, not typed out, so the copy cannot
            drift if a state is ever added. */}
        {NEVER_WRITTEN.size} of the {REAL_STATES.length + NEVER_WRITTEN.size} states in the
        contract are never written to a task document and are not listed above:{' '}
        {Array.from(NEVER_WRITTEN).join(', ')}. A row that can only ever read
        zero teaches that nothing is wrong, rather than that the bucket cannot
        fill.
      </p>
    </section>
  )
}
