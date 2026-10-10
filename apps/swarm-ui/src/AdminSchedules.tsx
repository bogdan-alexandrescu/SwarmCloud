/**
 * ADMIN › SCHEDULES (docs/schedules.md §5.3, §6.2 variant A: one table with a
 * tenant column), beside Tenants.
 *
 * Every tenant's schedules, sortable by the admin's questions -- tier, spend,
 * failures -- with the tick's health from the last hour of `schedule_ticks/`.
 * An admin may pause, disable or re-enable any schedule, each audited as
 * `admin:<email>`. An admin may NOT edit another tenant's parameters, gate,
 * scope or spec, and may not approve its approvals: those are the tenant's
 * decisions, so `_admin_row` does not even serve them and this page has no
 * control that could.
 */
import { useState } from 'react'
import { adminScheduleAction, loadAdminSchedules, type AdminScheduleRow, type AdminSchedules } from './api'
import { Banner, Button, Segmented } from './components'
import { Dash } from './components/Chip'
import type { ApiError } from './fetch'
import { Absent } from './primitives'
import { SpendCell, stateWord, whenIn } from './Schedules'
import { Screen } from './Shell'
import { pluralise, timeAgo } from './types'
import { useNow } from './useNow'
import './styles/automate.css'

const SORTS = ['tenant', 'tier', 'spend', 'pending'] as const
type Sort = (typeof SORTS)[number]

const TIER_ORDER: Readonly<Record<string, number>> = { R0: 0, R1: 1, R2: 2, R3: 3 }

function sorted(rows: readonly AdminScheduleRow[], by: Sort): AdminScheduleRow[] {
  const out = [...rows]
  if (by === 'tenant') out.sort((a, b) => a.tenant_id.localeCompare(b.tenant_id) || a.name.localeCompare(b.name))
  // Highest first: the question is "which is riskiest, dearest, most waited on".
  if (by === 'tier') out.sort((a, b) => (TIER_ORDER[b.tier ?? ''] ?? -1) - (TIER_ORDER[a.tier ?? ''] ?? -1))
  if (by === 'spend') out.sort((a, b) => (b.spend_today?.reported_usd ?? -1) - (a.spend_today?.reported_usd ?? -1))
  if (by === 'pending') out.sort((a, b) => b.pending_approvals - a.pending_approvals)
  return out
}

function tickWords(t: AdminSchedules['tick']): string {
  if (t === null || t === undefined) return 'the tick’s health was not served'
  const late = t.max_lateness_seconds === null ? 'lateness not measured' : `at most ${Math.round(t.max_lateness_seconds)} s late`
  return `tick: ${pluralise(t.ticks, 'run')} in the last ${t.window_minutes} min, ${pluralise(t.errors, 'error')}, ${t.fired} fired, ${late}`
}

export function AdminSchedulesScreen() {
  const [reloads, setReloads] = useState(0)
  return (
    <Screen
      key={`admin-schedules:${reloads}`}
      title="Schedules"
      load={async () => {
        const r = await loadAdminSchedules()
        return r.status === 'empty' ? { status: 'ok' as const, fetchedAt: r.fetchedAt, data: { schedules: [], next_page_token: null, tick: null } } : r
      }}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')} across tenants · ${tickWords(d.tick)}`}
    >
      {(d) => <AdminTable data={d} reread={() => setReloads((n) => n + 1)} />}
    </Screen>
  )
}

function AdminTable({ data, reread }: { data: AdminSchedules; reread: () => void }) {
  const now = useNow()
  const [by, setBy] = useState<Sort>('tenant')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  if (data.schedules.length === 0) {
    return <Absent kind="zero" heading="No tenant has a schedule" say="No schedule in any tenant: a real zero, read from the API" />
  }
  async function act(id: string, action: 'pause' | 'disable' | 'enable') {
    setBusy(`${id}:${action}`)
    setError(null)
    const r = await adminScheduleAction(id, action)
    setBusy(null)
    if (r.status === 'error') {
      setError(r.error)
      return
    }
    reread()
  }
  return (
    <div className="au-list">
      <div className="au-bar">
        <Segmented label="Sort by" value={by} options={SORTS.map((s) => ({ key: s, label: s }))} onChange={setBy} />
        {data.next_page_token !== null && <span className="au-dim">The first page; more schedules exist.</span>}
      </div>
      {error !== null && (
        <Banner tone="bad" title="Not changed">
          {error.code === null ? error.message : `${error.message} (${error.code})`}
        </Banner>
      )}
      <div className="au-table-wrap">
        <table className="au-table">
          <thead>
            <tr>
              <th scope="col">Tenant</th>
              <th scope="col">Name</th>
              <th scope="col">Type</th>
              <th scope="col">Tier</th>
              <th scope="col">When</th>
              <th scope="col">Next run</th>
              <th scope="col">Last outcome</th>
              <th scope="col">Spend today</th>
              <th scope="col">Waiting</th>
              <th scope="col">State</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sorted(data.schedules, by).map((s) => (
              <tr key={s.schedule_id} className="au-row" data-schedule={s.schedule_id}>
                <td data-label="Tenant" className="mono">{s.tenant_id}</td>
                <td data-label="Name">{s.name}</td>
                <td data-label="Type" className="mono">{s.type}</td>
                <td data-label="Tier">{s.tier ?? <Dash why="the type is no longer in the catalogue" />}</td>
                <td data-label="When">
                  {s.words ?? <Dash why={`the cron ${s.cron} did not parse`} />} <span className="au-dim">{s.timezone}</span>
                </td>
                <td data-label="Next run">
                  {s.state !== 'enabled' ? <Dash why={`not scheduled: ${stateWord(s.state).toLowerCase()}`} /> : whenIn(s.next_run_at, s.timezone) ?? <Dash why="not served" />}
                </td>
                <td data-label="Last outcome">
                  {s.last_outcome === null ? (
                    <span className="au-dim">never fired</span>
                  ) : (
                    <>
                      {s.last_outcome}
                      {s.last_firing_at !== null && <span className="au-dim"> · {timeAgo(s.last_firing_at, now)}</span>}
                    </>
                  )}
                </td>
                <td data-label="Spend today">
                  <SpendCell spend={s.spend_today} />
                </td>
                <td data-label="Waiting">{s.pending_approvals}</td>
                <td data-label="State" className={`au-state is-${s.state}`}>
                  {stateWord(s.state)}
                </td>
                <td data-label="Actions" className="au-row-acts">
                  {s.state === 'enabled' && (
                    <Button size="sm" busy={busy === `${s.schedule_id}:pause`} onClick={() => void act(s.schedule_id, 'pause')}>
                      Pause
                    </Button>
                  )}
                  {s.state !== 'disabled' ? (
                    <Button size="sm" kind="danger" busy={busy === `${s.schedule_id}:disable`} onClick={() => void act(s.schedule_id, 'disable')}>
                      Disable
                    </Button>
                  ) : (
                    <Button size="sm" busy={busy === `${s.schedule_id}:enable`} onClick={() => void act(s.schedule_id, 'enable')}>
                      Re-enable
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="au-dim">An admin pauses, disables or re-enables here. A tenant’s parameters, gate, scope and approvals are its own, and are not on this page.</p>
    </div>
  )
}
