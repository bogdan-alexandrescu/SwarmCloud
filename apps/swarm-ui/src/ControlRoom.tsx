import { useCallback, useEffect, useState } from 'react'
import { loadCapacity, loadDispatchControl, loadStats } from './api'
import { errorHeading, isPaused, type Result } from './fetch'
import { overCeiling, setBy, type Capacity, type Pool } from './types'

/**
 * O1 -- the Control Room. The landing screen.
 *
 * It answers one question: is the platform healthy right now, and if not,
 * what is the first thing to look at. Everything that does not serve that is
 * on another screen.
 */
export function ControlRoomScreen() {
  const [nonce, setNonce] = useState(0)
  const [stats, setStats] = useState<Result<unknown> | null>(null)
  const [capacity, setCapacity] = useState<Result<Capacity> | null>(null)
  const [dispatch, setDispatch] = useState<Result<{
    dispatch_paused: boolean
    updated_at: string | null
    updated_by: string | null
    reason: string | null
  }> | null>(null)
  const [counts, setCounts] = useState<Record<string, number> | null>(null)
  const [pausedFlag, setPausedFlag] = useState<boolean | null>(null)

  useEffect(() => {
    let live = true
    loadStats().then((r) => {
      if (!live) return
      setStats(r)
      if (r.status === 'ok' || r.status === 'stale') {
        setCounts(r.data.tasks_by_state)
        setPausedFlag(r.data.dispatch_paused)
      } else {
        // NOT false. "not paused" and "we could not read whether it is paused"
        // are different facts and the pill has a third state for exactly this.
        setCounts(null)
        setPausedFlag(null)
      }
    })
    loadCapacity().then((r) => live && setCapacity(r))
    loadDispatchControl().then((r) => live && setDispatch(r))
    return () => {
      live = false
    }
  }, [nonce])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  const ctl = dispatch?.status === 'ok' || dispatch?.status === 'stale' ? dispatch.data : null

  return (
    <>
      <div className="head">
        <h1>Control room</h1>
        <span className="env">dev</span>
      </div>

      <DispatchPill paused={pausedFlag} ctl={ctl} onRefresh={refresh} stats={stats} />

      <Counters counts={counts} capacity={capacity} />

      <PoolsNeedingAttention capacity={capacity} />
    </>
  )
}

function DispatchPill({
  paused,
  ctl,
  onRefresh,
  stats,
}: {
  paused: boolean | null
  ctl: { updated_by: string | null; updated_at: string | null; reason: string | null } | null
  onRefresh: () => void
  stats: Result<unknown> | null
}) {
  // Three states, not two. A failed stats read must not render as
  // "DISPATCHING" -- that is a confident green light nobody checked.
  const cls = paused === null ? 'unknown' : paused ? 'bad' : 'ok'
  const label =
    paused === null
      ? 'DISPATCH STATE UNKNOWN'
      : paused
        ? 'DISPATCH PAUSED'
        : 'DISPATCHING'

  return (
    <div className={`banner pill ${cls}`}>
      <strong>{label}</strong>
      {paused === null && stats?.status === 'error' && (
        <> — {errorHeading(stats.error)}: {stats.error.message}</>
      )}
      {paused === true && ctl?.updated_by && (
        <>
          {' '}— paused by {ctl.updated_by}
          {ctl.updated_at && ` · ${new Date(ctl.updated_at).toLocaleTimeString()}`}
          {ctl.reason && ` · “${ctl.reason}”`}
        </>
      )}
      <button className="pill-refresh" onClick={onRefresh}>
        refresh
      </button>
    </div>
  )
}

function Counters({
  counts,
  capacity,
}: {
  counts: Record<string, number> | null
  capacity: Result<Capacity> | null
}) {
  const cap = capacity?.status === 'ok' || capacity?.status === 'stale' ? capacity.data : null
  const global = cap?.pools.find((p) => p.name === 'global')

  return (
    <div className="tiles">
      <Counter
        label="Holding capacity"
        value={global ? String(global.active) : null}
        sub="units on the global pool — every lease takes it"
      />
      <Counter
        label="Waiting"
        value={counts ? String((counts['READY'] ?? 0) + (counts['QUEUED'] ?? 0)) : null}
        sub="READY + QUEUED · costs nothing"
      />
      <Counter label="Parked" value={counts ? String(counts['PARKED'] ?? 0) : null} sub="durable, free" />
      <Counter
        label="Currently dead-lettered"
        value={counts ? String(counts['DEAD_LETTERED'] ?? 0) : null}
        // Never "all time": count_tasks_by_state counts tasks IN that state
        // now, and purge-data.sh drops the whole tasks collection, so an
        // all-time framing is wrong the first time anyone runs a purge.
        sub="in that state now, not all time"
        alert={Boolean(counts && (counts['DEAD_LETTERED'] ?? 0) > 0)}
      />
    </div>
  )
}

function Counter({
  label,
  value,
  sub,
  alert,
}: {
  label: string
  value: string | null
  sub: string
  alert?: boolean
}) {
  return (
    <div className={`tile${alert ? ' alert' : ''}${value === null ? ' blocked' : ''}`}>
      <span className="t-label">{label}</span>
      {/* A failed read renders "unreadable", never 0. A zero here is the most
          reassuring thing on the screen and it would be a guess. */}
      <span className="t-value">{value ?? 'unreadable'}</span>
      <span className="t-sub">{sub}</span>
    </div>
  )
}

/**
 * Row 3 -- pools that need looking at. Not all of them.
 *
 * Oversubscribed is its OWN group and the loudest thing here, because it is
 * the one symptom of the capacity-leak class this screen can show without a
 * leases route: a pool holding more than its own ceiling allows. Folding it
 * into "full" hides it, since both have available == 0.
 */
function PoolsNeedingAttention({ capacity }: { capacity: Result<Capacity> | null }) {
  if (capacity === null) return <div className="skeleton" style={{ height: 80 }} />
  if (capacity.status === 'error') {
    return (
      <div className="state failed">
        <h3>{errorHeading(capacity.error)}</h3>
        <p>
          Pool state could not be read, so this screen cannot say whether any
          pool needs attention. That is not the same as none needing it.
        </p>
      </div>
    )
  }
  if (capacity.status === 'loading' || capacity.status === 'empty') return null

  const pools = capacity.data.pools
  const over = pools.filter(overCeiling)
  const full = pools.filter((p) => !overCeiling(p) && p.available === 0 && !isPaused(p))
  const drained = pools.filter(isPaused)

  if (over.length === 0 && full.length === 0 && drained.length === 0) {
    return (
      <section className="section">
        <h2>Pools</h2>
        <p className="muted">
          No pool is oversubscribed, full or drained. This is a real all-clear
          from a successful read of {pools.length} pools.
        </p>
      </section>
    )
  }

  return (
    <>
      {over.length > 0 && (
        <PoolGroup
          title="Oversubscribed"
          tone="bad"
          pools={over}
          note="Holding more than its own ceiling allows. Admission cannot produce this, so it is a limit lowered under running work or a slot never released."
        />
      )}
      {full.length > 0 && <PoolGroup title="Full" tone="warn" pools={full} note="No headroom. New work waits." />}
      {drained.length > 0 && (
        <PoolGroup
          title="Drained — no new admissions"
          tone="paused"
          pools={drained}
          note="Paused by an operator. The count beside each is the pool counter, unverified — a drained pool that never reaches zero is either work still finishing or a leaked lease, and this screen cannot tell which."
        />
      )}
    </>
  )
}

function PoolGroup({
  title,
  tone,
  pools,
  note,
}: {
  title: string
  tone: string
  pools: Pool[]
  note: string
}) {
  return (
    <section className={`section pool-group ${tone}`}>
      <h2>
        {title} <span className="count-chip">{pools.length}</span>
      </h2>
      {pools.map((p) => {
        const by = setBy(p)
        const ratio = p.effective_limit > 0 ? Math.min(1.4, p.active / p.effective_limit) : 1
        return (
          <div className="split-row" key={p.name}>
            <span className="sr-name" title={p.name}>
              {p.name}
            </span>
            <span className="sr-bar">
              <i className={tone === 'bad' ? 'bad' : ''} style={{ width: `${Math.min(100, ratio * 100)}%` }} />
            </span>
            <span className="sr-n">
              {p.active}/{p.effective_limit}
            </span>
            <span className="sr-by" title={by.detail}>
              {by.term}
            </span>
          </div>
        )
      })}
      <p className="muted small">{note}</p>
    </section>
  )
}
