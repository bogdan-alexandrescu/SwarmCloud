import { useEffect, useState } from 'react'
import { loadLeases } from './api'
import { errorHeading, num, type ApiError, type Result } from './fetch'
import { timeAgo } from './Shell'
import { leaseLiveliness, type LeasePage, type LeaseRow } from './types'

/**
 * Silent workers, and admitted-but-never-dispatched.
 *
 * The spec calls the first the freshest failure signal on the platform, and
 * it is: this sees a quiet worker up to five minutes before the reconciler
 * acts on it. Both panels come from ONE request, because they are the same
 * query with different filters — so when it fails they fail together, and
 * say so, rather than showing two independent-looking blanks.
 *
 * THE THRESHOLDS ARE NOT IN THIS FILE. They arrive with the data, because 90
 * and 120 belong to the reconciler and the grace is derived from the
 * heartbeat interval rather than being a constant. Colouring from a local
 * copy would mean marking a row amber at a threshold the reconciler does not
 * act on.
 */
export function LeasesPanel() {
  const [state, setState] = useState<Result<LeasePage>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let live = true
    loadLeases().then((r) => live && setState(r))
    return () => {
      live = false
    }
  }, [nonce])

  // Ten seconds: this is the cheapest live signal on the platform, bounded by
  // live concurrency rather than history.
  useEffect(() => {
    const poll = setInterval(() => setNonce((n) => n + 1), 10_000)
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [])

  if (state.status === 'loading') return <div className="skeleton" style={{ height: 90 }} />
  if (state.status === 'error') return <LeaseError error={state.error} />
  if (state.status === 'empty') {
    return (
      <section className="section panel">
        <h2>Capacity holders</h2>
        <p className="muted">
          No unreleased leases — nothing is holding capacity right now. This
          sentence is only legitimate after a successful read, and this was one.
        </p>
      </section>
    )
  }

  const page = state.data
  const overdue = page.leases.filter((l) => l.dispatch_state === 'LEASED' && l.dispatch_overdue)
  const holders = page.leases.filter((l) => !overdue.includes(l))

  return (
    <>
      <SilentWorkers rows={holders} page={page} now={now} />
      <NeverDispatched rows={overdue} page={page} />
    </>
  )
}

function LeaseError({ error }: { error: ApiError }) {
  if (error.kind === 'admin_required') {
    return (
      <section className="section panel">
        <h2>Capacity holders</h2>
        <p className="muted admin-only">
          Admin only — lease state is platform-wide, so it is not shown to a
          non-admin. Nothing failed.
        </p>
      </section>
    )
  }
  return (
    <section className="section panel">
      <h2>Capacity holders</h2>
      <div className="state failed">
        <h3>{errorHeading(error)}</h3>
        {/* Both panels share one request, so they are blind together and it
            would be dishonest to render one of them as merely empty. */}
        <p>
          Lease query failed ({error.code ?? error.kind}) — <strong>both lease
          panels are blind</strong>. No green tick anywhere on this card.
        </p>
      </div>
    </section>
  )
}

function SilentWorkers({
  rows,
  page,
  now,
}: {
  rows: LeaseRow[]
  page: LeasePage
  now: number
}) {
  // Longest silent first, so the worst row is the top row without scrolling.
  const sorted = [...rows].sort((a, b) => b.silent_seconds - a.silent_seconds)
  const drift = Math.max(0, Math.round((now - new Date(page.evaluated_at).getTime()) / 1000))

  return (
    <section className="section panel">
      <h2>
        Capacity holders
        <span className="count-chip">{page.units_held} units</span>
      </h2>
      {sorted.length === 0 ? (
        <p className="muted">Every unreleased lease has reached its backend.</p>
      ) : (
        <div className="table-wrap">
          <table className="pools">
            <thead>
              <tr>
                <th scope="col">Task</th>
                <th scope="col">Dispatch</th>
                <th scope="col" className="n">Silent for</th>
                <th scope="col">Lease expires</th>
                <th scope="col" className="n">Gen</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((l) => {
                const live = leaseLiveliness(l, page.thresholds)
                return (
                  <tr key={l.lease_id} className={live.kind}>
                    <th scope="row" className="pool-name">
                      {l.task_id.slice(-10)}
                      <span className="raw">{l.tenant_id}</span>
                    </th>
                    {/* "dispatch", never "state" — nothing writes STARTING or
                        RUNNING here, so this would read DISPATCHED for an
                        agent that has been running for an hour. */}
                    <td>{l.dispatch_state === 'LEASED' ? 'awaiting dispatch' : 'dispatched'}</td>
                    <td className="n" title={live.copy}>
                      <span className={`silent ${live.kind}`}>
                        {fmt(l.silent_seconds + drift)}
                      </span>
                      {!l.heartbeat_ever && <span className="raw">never beat</span>}
                    </td>
                    <td>{timeAgo(l.expires_at)}</td>
                    <td className="n">{l.generation}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <Legend page={page} />
    </section>
  )
}

/** Stated on the screen, from the payload — never from a constant here. */
function Legend({ page }: { page: LeasePage }) {
  const { heartbeat_grace_seconds: grace, lease_timeout_seconds: ttl } = page.thresholds
  return (
    <>
      <dl className="kv legend-kv">
        <dt className="alive">alive</dt>
        <dd>Silent under {grace}s. Nothing will touch this.</dd>
        <dt className="silent">silent</dt>
        <dd>
          {grace}s or more. <strong>This is already the reconciler&apos;s
          trigger</strong> — its next pass will reclaim the lease, and that pass
          is up to five minutes away.
        </dd>
        <dt className="presumed-dead">presumed dead</dt>
        <dd>
          Past <code>expires_at</code> ({ttl}s TTL, re-extended on every
          heartbeat). A second, independent signal — not a later stage of the
          first.
        </dd>
      </dl>
      <p className="muted small">
        Fresher than the reconciler; blind to the backend. This tells you a
        slot is held by something silent. It cannot tell you <em>why</em> the
        worker went quiet, or whether an execution is still running — only the
        reconciler calls the backend, and it discards what it learns.
        Thresholds come from the API, so they always match what the reconciler
        acts on.
      </p>
    </>
  )
}

function NeverDispatched({ rows, page }: { rows: LeaseRow[]; page: LeasePage }) {
  return (
    <section className="section panel">
      <h2>
        Admitted but never dispatched
        {rows.length > 0 && <span className="count-chip">{rows.length}</span>}
      </h2>
      {rows.length === 0 ? (
        <p className="muted">
          Nothing admitted is overdue to start — every lease taken has reached
          its backend.
        </p>
      ) : (
        <>
          <p className="headline">
            A slot reserved for a container that never started.
          </p>
          {rows.map((l) => (
            <div className="q-row" key={l.lease_id}>
              <span className="q-reason">{l.task_id.slice(-10)}</span>
              <span className="q-count">
                overdue by {fmt(secondsPast(l.dispatch_deadline))}
              </span>
              <span className="q-pool">{l.tenant_id}</span>
              {/* Never ellipsised: a truncated stable code is a useless
                  stable code, and the upstream text is deliberately not here
                  because it echoes service account and secret names. */}
              {l.last_error && <pre className="err">{l.last_error}</pre>}
              <span className="client-side">
                The full message is in the scheduler logs — give an operator{' '}
                <code>{l.attempt_id}</code>.
              </span>
            </div>
          ))}
        </>
      )}
      <p className="provenance">
        evaluated {timeAgo(page.evaluated_at)} · thresholds from the API
      </p>
    </section>
  )
}

function secondsPast(iso: string): number {
  const t = new Date(iso).getTime()
  return Number.isFinite(t) ? Math.max(0, Math.round((Date.now() - t) / 1000)) : 0
}

function fmt(s: number): string {
  if (!Number.isFinite(s)) return num(null)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}:${String(r).padStart(2, '0')}` : `${r}s`
}
