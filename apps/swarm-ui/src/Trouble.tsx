import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  loadCapacity,
  loadDispatchControl,
  loadProviders,
  loadStats,
  loadTasksInState,
} from './api'
import { errorHeading, isPaused, num, type ApiError, type Result } from './fetch'
import { timeAgo } from './Shell'
import {
  NEEDS_A_HUMAN,
  PRE_CAPACITY_REASONS,
  providerTone,
  reasonCopy,
  type Capacity,
  type DispatchControl,
  type ProviderEntry,
  type ProvidersPage,
  type Stats,
  type Task,
  type TaskPage,
} from './types'

/**
 * The Trouble board. What an admin opens at 3am, and what a user opens when
 * their task "isn't doing anything".
 *
 * Seven independent fetches. One failing must not blank the page, and the page
 * must not look complete when one has — so the header counts failures.
 *
 * NOT BUILT, and deliberately: "silent workers" (§1.2) and "admitted but never
 * dispatched" (§1.3). Both need the `leases` collection, which has no API read
 * path at all. They are the freshest failure signals on the platform and they
 * are unreachable; a panel faked from task state would be guessing.
 */
export function TroubleScreen() {
  const stats = usePanel(loadStats)
  const dispatch = usePanel(loadDispatchControl)
  const failed = usePanel(useCallback(() => loadTasksInState('FAILED'), []))
  const parked = usePanel(useCallback(() => loadTasksInState('PARKED'), []))
  const ready = usePanel(useCallback(() => loadTasksInState('READY'), []))
  const capacity = usePanel(loadCapacity)
  const providers = usePanel(loadProviders)

  const panels = [stats, dispatch, failed, parked, ready, capacity, providers]
  // A 403 on an admin panel is not a failure. The disambiguation is
  // POSITIONAL, not textual: Forbidden is also raised for the wrong
  // organisation and carries the same code.
  const failures = panels.filter(
    (p) => p.state.status === 'error' && p.state.error.kind !== 'admin_required',
  ).length
  const allForbidden =
    panels.every((p) => p.state.status === 'error' && p.state.error.httpStatus === 403)
  const sessionGone = panels.some(
    (p) =>
      p.state.status === 'error' &&
      (p.state.error.kind === 'unauthenticated' || p.state.error.kind === 'session_expired'),
  )

  // A 401 is a page-level state, not a panel-level one: an expired session
  // fails all seven at once, and rendering seven independently empty panels is
  // exactly the bug this board is built against.
  if (sessionGone) {
    return (
      <div className="state failed">
        <h3>Your session expired</h3>
        <p>Every panel on this board failed to authenticate. Reload to sign in again.</p>
        <button className="retry" onClick={() => window.location.reload()}>
          Reload to sign in
        </button>
      </div>
    )
  }
  if (allForbidden) {
    const first = panels.find((p) => p.state.status === 'error')
    return (
      <div className="state failed">
        <h3>This account is not permitted here</h3>
        <p>
          {first?.state.status === 'error'
            ? first.state.error.message
            : 'Every panel returned 403.'}
        </p>
      </div>
    )
  }

  return (
    <>
      <div className="head">
        <h1>Trouble</h1>
        <span className="env">dev</span>
      </div>

      <PlatformBanner stats={stats.state} dispatch={dispatch.state} failures={failures} />

      <Panel title="Failures that need a human" p={failed} onRetry={failed.retry}>
        {(page, meta) => <Failures page={page} meta={meta} />}
      </Panel>

      <Panel title="Parked work" p={parked} onRetry={parked.retry}>
        {(page, meta) => <Parked page={page} meta={meta} />}
      </Panel>

      <QueuePanel ready={ready} capacity={capacity} />

      <Panel title="Provider health" p={providers} onRetry={providers.retry}>
        {(d, meta) => <Providers page={d} meta={meta} />}
      </Panel>
    </>
  )
}

// ---------------------------------------------------------------------------
// Panel plumbing
// ---------------------------------------------------------------------------

interface PanelHandle<T> {
  state: Result<T>
  retry: () => void
}

function usePanel<T>(load: () => Promise<Result<T>>): PanelHandle<T> {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    load().then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, load])

  return { state, retry: useCallback(() => setNonce((n) => n + 1), []) }
}

/** `source · fetched 14:03:07 · 12 rows`. Mandatory and always visible. */
interface Provenance {
  rows: number
  fetchedAt: number
}

function ProvenanceLine({ meta, of }: { meta: Provenance; of?: boolean }) {
  return (
    <p className="provenance">
      fetched {timeAgo(meta.fetchedAt)} · {meta.rows} row{meta.rows === 1 ? '' : 's'}
      {/* Never a literal page size in the template: default_page_size and
          max_page_size are both environment-overridable, so a hardcoded "50"
          is a lie one DEFAULT_PAGE_SIZE change away. */}
      {of && ' returned'}
    </p>
  )
}

function Panel<T>({
  title,
  p,
  onRetry,
  children,
}: {
  title: string
  p: PanelHandle<T>
  onRetry: () => void
  children: (data: T, meta: Provenance) => ReactNode
}) {
  const s = p.state
  return (
    <section className="section panel">
      <h2>{title}</h2>
      {s.status === 'loading' && <div className="skeleton" style={{ height: 64 }} />}
      {s.status === 'error' && <PanelError error={s.error} onRetry={onRetry} />}
      {s.status === 'empty' && (
        <p className="muted">
          The read succeeded and returned nothing.{' '}
          <span className="provenance">checked {timeAgo(s.fetchedAt)}</span>
        </p>
      )}
      {(s.status === 'ok' || s.status === 'stale') &&
        children(s.data, { rows: 0, fetchedAt: s.fetchedAt })}
    </section>
  )
}

/**
 * A 403 on an admin panel renders as information, never as a red failure. A
 * non-admin genuinely cannot read /v1/admin/*.
 */
function PanelError({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  if (error.kind === 'admin_required') {
    return (
      <p className="muted admin-only">
        Admin only — you are not in an admin group, so this panel has nothing to
        show you. That is not a failure.
      </p>
    )
  }
  if (error.kind === 'rate_limited') {
    return (
      <p className="warn-text">
        {errorHeading(error)} — retrying in {num(error.retryAfterSeconds ?? null)}s.
      </p>
    )
  }
  return (
    <div className="state failed">
      <h3>{errorHeading(error)}</h3>
      {/* The code and message from the envelope, and NO NUMBER. Not 0, not a
          bare em dash: a zero here reads as reassurance. */}
      <p>
        {error.code ?? 'error'} — {error.message}
      </p>
      <button className="retry" onClick={onRetry}>
        Try again
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1.1 Platform state banner
// ---------------------------------------------------------------------------

function PlatformBanner({
  stats,
  dispatch,
  failures,
}: {
  stats: Result<Stats>
  dispatch: Result<DispatchControl>
  failures: number
}) {
  // The single most important place not to render zeros: "0 RUNNING" and
  // "stats failed" are opposite facts.
  if (stats.status === 'error') {
    return (
      <div className="banner bad">
        PLATFORM STATE UNKNOWN — stats query failed ({stats.error.code ?? stats.error.kind})
      </div>
    )
  }
  if (stats.status === 'loading') return <div className="banner skeleton" style={{ height: 38 }} />

  const data = stats.status === 'empty' ? null : stats.data
  if (!data) return null

  const admin = data.platform_tasks_by_state !== undefined
  const counts = data.tasks_by_state
  const show = ['LEASED', 'RUNNING', 'READY', 'PARKED'] as const

  const ctl = dispatch.status === 'ok' || dispatch.status === 'stale' ? dispatch.data : null

  return (
    <>
      {data.dispatch_paused && (
        <div className="banner bad">
          <strong>DISPATCH PAUSED</strong>
          {ctl?.updated_by && (
            <>
              {' '}— paused by {ctl.updated_by}
              {ctl.updated_at && ` at ${new Date(ctl.updated_at).toLocaleTimeString()}`}
              {ctl.reason && ` — “${ctl.reason}”`}
            </>
          )}
        </div>
      )}
      <div className="banner neutral">
        {/* The scope word is not decoration. An admin reading their own
            tenant's four running tasks as the platform total is a truth bug. */}
        <span className="scope-word">{admin ? 'YOURS:' : 'YOURS:'}</span>{' '}
        {show.map((k) => (
          <span key={k} className="count-chip">
            {counts[k] ?? 0} {k}
          </span>
        ))}
      </div>
      {failures > 0 && (
        <div className="banner warn">
          {failures} of 7 panels failed — this board is incomplete.
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// 1.4 Failures that need a human
// ---------------------------------------------------------------------------

function Failures({ page, meta }: { page: TaskPage; meta: Provenance }) {
  const rows = page.tasks
  // Computed client-side because Firestore cannot compare two fields in a
  // query. Always phrased "of the last N" -- a bare "3 tasks need attention"
  // becomes a lie the moment there is an N+1th.
  const exhausted = rows.filter((t) => t.attempt_count >= t.max_attempts).length

  return (
    <>
      <p className="headline">
        <strong>{exhausted}</strong> of the last {rows.length} failed tasks have
        exhausted their attempts.
      </p>
      <div className="rows">
        {rows.map((t) => (
          <div className="row fail-row" key={t.id}>
            <span className="id">{t.id.slice(-8)}</span>
            <span className="meta">{t.runner_profile}</span>
            <span className={`try${t.attempt_count >= t.max_attempts ? ' spent' : ''}`}>
              {t.attempt_count}/{t.max_attempts}
            </span>
            <span className="when">{t.completed_at ? timeAgo(t.completed_at) : '—'}</span>
            {t.last_error && <pre className="err">{t.last_error}</pre>}
          </div>
        ))}
      </div>
      <ProvenanceLine meta={{ ...meta, rows: rows.length }} of />
      <p className="muted small">
        Tenant-scoped, and a page rather than a total. There is no
        DEAD_LETTERED tab: that state exists in the contract and no code path
        ever writes it, so a tab for it would be permanently empty and
        indistinguishable from nothing being wrong.
      </p>
    </>
  )
}

// ---------------------------------------------------------------------------
// 1.5 Parked work, by reason -- grey, never red
// ---------------------------------------------------------------------------

function Parked({ page, meta }: { page: TaskPage; meta: Provenance }) {
  const rows = page.tasks
  const byReason = new Map<string, Task[]>()
  for (const t of rows) {
    const key = t.park_reason ?? 'unspecified'
    const list = byReason.get(key)
    if (list) list.push(t)
    else byReason.set(key, [t])
  }
  const entries = Array.from(byReason.entries()).sort((a, b) => b[1].length - a[1].length)
  const total = rows.length || 1

  return (
    <>
      <p className="headline grey">
        Parked work costs nothing. A tall bar here is the platform declining to
        burn compute on a wait.
      </p>
      <div className="stack" role="img" aria-label="Parked work by reason">
        {entries.map(([reason, list]) => (
          <i
            key={reason}
            className={NEEDS_A_HUMAN.has(reason) ? 'human' : ''}
            style={{ width: `${(list.length / total) * 100}%` }}
            title={`${reason}: ${list.length}`}
          />
        ))}
      </div>
      <dl className="kv">
        {entries.map(([reason, list]) => (
          <div key={reason} style={{ display: 'contents' }}>
            <dt className={NEEDS_A_HUMAN.has(reason) ? 'needs-human' : undefined}>
              {/* Render the enum value; an unrecognised string renders
                  verbatim rather than being dropped. */}
              {reason} ({list.length})
            </dt>
            <dd>{reasonCopy(reason)}</dd>
          </div>
        ))}
      </dl>
      <ProvenanceLine meta={{ ...meta, rows: rows.length }} of />
    </>
  )
}

// ---------------------------------------------------------------------------
// 1.6 Why the queue is not moving
// ---------------------------------------------------------------------------

function QueuePanel({
  ready,
  capacity,
}: {
  ready: PanelHandle<TaskPage>
  capacity: PanelHandle<Capacity>
}) {
  const s = ready.state
  return (
    <section className="section panel">
      <h2>Why the queue is not moving</h2>
      {s.status === 'loading' && <div className="skeleton" style={{ height: 64 }} />}
      {s.status === 'error' && <PanelError error={s.error} onRetry={ready.retry} />}
      {s.status === 'empty' && (
        <p className="muted">
          No READY task was refused on the last pass — the queue is moving.
        </p>
      )}
      {(s.status === 'ok' || s.status === 'stale') && (
        <Queue page={s.data} capacity={capacity.state} fetchedAt={s.fetchedAt} />
      )}
    </section>
  )
}

function Queue({
  page,
  capacity,
  fetchedAt,
}: {
  page: TaskPage
  capacity: Result<Capacity>
  fetchedAt: number
}) {
  const blocked = page.tasks.filter((t) => t.blocked_by && t.blocked_by.length > 0)
  if (blocked.length === 0) {
    return (
      <p className="muted">
        No READY task was refused on the last pass — the queue is moving.
      </p>
    )
  }

  // Group by reason, keeping the numbers AS THEY WERE AT REFUSAL. Pairing a
  // blocker with a live pool reading taken twenty seconds later produces rows
  // like "TENANT_LIMIT - 8 tasks - 14/20 active", which reads as a
  // contradiction and is really two timestamps in one sentence.
  const groups = new Map<string, { count: number; pool?: string; limit?: number; active?: number }>()
  for (const t of blocked) {
    const b = t.blocked_by?.[0]
    if (!b) continue
    const g = groups.get(b.reason) ?? { count: 0, pool: b.pool, limit: b.limit, active: b.active }
    g.count++
    groups.set(b.reason, g)
  }

  const live =
    capacity.status === 'ok' || capacity.status === 'stale'
      ? new Map(capacity.data.pools.map((p) => [p.name, p]))
      : null

  const entries = Array.from(groups.entries()).sort((a, b) => b[1].count - a[1].count)
  const preCapacity = entries.filter(([r]) => PRE_CAPACITY_REASONS.has(r))
  const capacityRefusals = entries.filter(([r]) => !PRE_CAPACITY_REASONS.has(r))

  return (
    <>
      {capacityRefusals.map(([reason, g]) => {
        const pool = g.pool && live ? live.get(g.pool) : undefined
        const drained = pool ? isPaused(pool) : false
        return (
          <div className="q-row" key={reason}>
            <span className="q-reason">{reason}</span>
            <span className="q-count">{g.count} tasks</span>
            {g.pool && (
              <span className="q-pool">
                pool “{g.pool}”{' '}
                {typeof g.active === 'number' && typeof g.limit === 'number'
                  ? `${g.active}/${g.limit} at refusal`
                  : 'at refusal'}
              </span>
            )}
            {live === null ? (
              // Never an ABSENT chip -- absence reads as "not drained".
              <span className="q-chip unknown">live pool state unavailable</span>
            ) : drained ? (
              <span className="q-chip bad">DRAINED now</span>
            ) : null}
          </div>
        )
      })}

      {preCapacity.length > 0 && (
        <>
          <h3 className="sub-h">Refused before capacity was evaluated</h3>
          {preCapacity.map(([reason, g]) => (
            <div className="q-row" key={reason}>
              <span className="q-reason">{reason}</span>
              <span className="q-count">{g.count} tasks</span>
            </div>
          ))}
        </>
      )}

      <ProvenanceLine meta={{ rows: blocked.length, fetchedAt }} of />
      <p className="muted small">
        Counts are over the {page.tasks.length} most recently created READY
        tasks, not a total: <code>blocked_by</code> is an array of maps and
        Firestore cannot equality-filter one field of an element, so no
        per-reason count exists. A drained pool is recorded as{' '}
        <code>MANUAL_PAUSE</code>, not <code>RUNNER_LIMIT</code> — the enabled
        check runs before the capacity check.
      </p>
    </>
  )
}

// ---------------------------------------------------------------------------
// 1.7 Provider health
// ---------------------------------------------------------------------------

function Providers({ page, meta }: { page: ProvidersPage; meta: Provenance }) {
  return (
    <>
      <div className="grid">
        {page.providers.map((p) => (
          <ProviderCard key={p.provider} entry={p} />
        ))}
      </div>
      <ProvenanceLine meta={{ ...meta, rows: page.providers.length }} />
      <p className="muted small">
        Quota is per provider <em>per tenant</em> — tenants bring their own
        keys, so one tenant&apos;s 429 is not a platform outage. Whether a
        provider is down globally is not measured anywhere.
      </p>
    </>
  )
}

function ProviderCard({ entry }: { entry: ProviderEntry }) {
  const q = entry.quota
  const state = q?.state ?? 'UNKNOWN'

  return (
    <div className={`pool prov ${providerTone(state)}`}>
      <div className="top">
        <span className="name">{entry.provider}</span>
        <span className={`tag ${providerTone(state)}`}>{state}</span>
      </div>
      <div className="prov-meta">
        credential {entry.credential_registered ? 'registered' : 'not registered'} ·{' '}
        {entry.runner_profiles.join(', ')}
      </div>
      {q === null ? (
        <p className="muted small">
          No quota reported for your tenant yet. That is not the same as
          healthy — no worker has reported on this provider.
        </p>
      ) : (
        <dl className="kv">
          <dt>Effective limit</dt>
          {/* The one zero on this board that must render AS a zero: it
              deliberately returns 0 when EXHAUSTED, DISABLED or COOLDOWN, and
              the state chip beside it does the explaining. */}
          <dd>{q.effective_limit}</dd>
          <dt>Requests left</dt>
          <dd>{num(q.requests_remaining)}</dd>
          <dt>Tokens left</dt>
          <dd>{num(q.tokens_remaining)}</dd>
          <dt>Last 429</dt>
          <dd>{q.last_429_at ? timeAgo(q.last_429_at) : '—'}</dd>
          <dt>429 / success</dt>
          <dd>
            {q.rate_limit_count} / {q.success_count}
          </dd>
          {q.cooldown_until && (
            <>
              <dt>Cooldown until</dt>
              <dd>{new Date(q.cooldown_until).toLocaleTimeString()}</dd>
            </>
          )}
        </dl>
      )}
    </div>
  )
}
