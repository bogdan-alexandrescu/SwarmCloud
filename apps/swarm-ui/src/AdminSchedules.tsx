/**
 * AUTOMATE › SCHEDULES, ALL TENANTS (docs/schedules.md §5.3, §6.1-§6.2, lane
 * S7): every tenant's schedules in one table with a tenant column (§6.2
 * variant A), because sorting by tier, spend or failures across tenants is the
 * admin's question; and the schedule tick's last hour.
 *
 * NOT A PAGE OF ITS OWN ANY MORE (SCHED-ONE-PAGE, owner decision 2026-10-13).
 * This was Admin › Schedules, and the owner asked "why do I have schedules in
 * Admin and also in Automate?" -- two nav entries for one noun. It is now the
 * "All tenants" side of the one Schedules page (`Schedules.tsx` draws the
 * switch, for an admin only, and owns `?scope=all`); this file keeps the
 * cross-tenant read and the admin's actions so they are not copied there.
 * `/admin/schedules` lands on `/automate/schedules?scope=all`. The platform's
 * own ticks (§8.4) went to Admin › Platform counts (`PlatformCounts.tsx`):
 * they belong to no tenant, so they do not sit behind "Mine | All tenants".
 *
 * WHAT AN ADMIN MAY DO HERE, AND WHAT NOT. Pause, disable and re-enable any
 * schedule, and pause all of one tenant's, typed with that tenant's id --
 * each audited as `admin:<email>` by the server. Not edit another tenant's
 * parameters, gate, scope or spec, and not approve its approvals: those are
 * the tenant's decisions, and an admin's console session is not that
 * tenant's identity (§5.3); `GET /v1/admin/schedules` serves none of those
 * fields. A row's name links to the schedule's own page only when its tenant
 * is the one this console is working in (AUT-18), because that page reads
 * `GET /v1/schedules/{id}` as the working tenant: any other tenant's row says
 * to switch tenant first, rather than linking to a page that would 404.
 *
 * Every word a reader sees says workspace, never tenant (VQ-056, BGD
 * 2026-10-26); the routes, `tenant_id` and `data-col="tenant"` (a CSS hook)
 * keep their names.
 *
 * A pause never cancels a tenant's live runs from here: that is the tenant's
 * `:pause` with `cancel_live`, or an admin's ordinary cancel of the work.
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  adminPauseTenantSchedules,
  adminScheduleAction,
  loadAdminSchedules,
  loadMe,
  type AdminScheduleRow,
  type ScheduleTickHealth,
} from './api'
import { Button, Dialog, TypedConfirm } from './components'
import { type ApiError } from './fetch'
import { Absent } from './primitives'
import { InApp, STATE_WORD, SpendFigure, localTime, scheduleAddress, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

type Action = 'pause' | 'disable' | 'enable'

const ACTION_WORD: Readonly<Record<Action, string>> = { pause: 'Pause', disable: 'Disable', enable: 'Re-enable' }

const ACTION_SAYS: Readonly<Record<Action, string>> = {
  pause: 'It stops firing until its workspace resumes it. Live work is not cancelled.',
  disable: 'It stops firing, and only an admin can enable it again; its workspace cannot resume it. Live work is not cancelled.',
  enable: 'It fires again from its next slot. Only a disabled schedule is re-enabled here: a workspace’s own pause is the workspace’s to lift.',
}

/** The actions a row offers in its state (§1.3, §5.3). */
function actionsFor(state: AdminScheduleRow['state']): Action[] {
  if (state === 'disabled') return ['enable']
  if (state === 'paused') return ['disable']
  return ['pause', 'disable']
}

/**
 * THE TICK'S LATENESS IS A DURATION (QA14-UI, owner QA 2026-10-13): the
 * server's float was printed as it came, "lateness 182.216849 s at most".
 * Under a minute it is whole seconds; from a minute, whole minutes; from an
 * hour, hours and minutes.
 */
export function latenessWords(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min`
  return m % 60 === 0 ? `${Math.floor(m / 60)} h` : `${Math.floor(m / 60)} h ${m % 60} min`
}

function TickHealth({ tick }: { tick: ScheduleTickHealth }) {
  if (tick.ticks === 0) {
    return (
      <Absent kind="zero" heading="No tick reported" say={`The schedule tick wrote no report in the last ${tick.window_minutes} minutes.`}>
        No schedule fires while the tick does not run. Its Cloud Scheduler job is <code>schedule-tick</code>.
      </Absent>
    )
  }
  return (
    <p className="sb-note" data-testid="as-tick">
      Last {tick.window_minutes} min: {pluralise(tick.ticks, 'tick')}, {tick.fired} fired, {pluralise(tick.errors, 'error')}
      {tick.disabled > 0 ? `, ${tick.disabled} with schedules switched off` : ''}
      {tick.truncated > 0 ? `, ${tick.truncated} cut short` : ''} · lateness{' '}
      {tick.max_lateness_seconds === null ? <i className="ctl-em">&mdash; none measured</i> : `${latenessWords(tick.max_lateness_seconds)} at most`}
    </p>
  )
}

function ActionDialog({ row, action, onClose, onDone }: { row: AdminScheduleRow; action: Action; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const send = async () => {
    setBusy(true)
    setRefused(null)
    const r = await adminScheduleAction(row.schedule_id, action, action === 'enable' ? undefined : reason)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'empty') return onDone()
    if (r.status === 'error') setRefused(r.error)
  }
  return (
    <Dialog
      title={`${ACTION_WORD[action]} ${row.name}?`}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>
            Leave it {STATE_WORD[row.state]}
          </Button>
          <Button kind={action === 'enable' ? 'primary' : 'danger'} busy={busy ? 'Sending…' : false} onClick={() => void send()}>
            {ACTION_WORD[action]}
          </Button>
        </>
      }
    >
      <p>
        {row.tenant_id} · {row.type}. {ACTION_SAYS[action]} The change is written to the schedule’s audit as yours.
      </p>
      {action !== 'enable' && (
        <label className="c-field">
          <span className="c-lbl">Reason (shown to the workspace)</span>
          <input className="c-inp" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}
      {refused !== null && (
        <p className="c-hint" role="alert" data-code={refused.code ?? ''}>
          Not done: {refused.message}
        </p>
      )}
    </Dialog>
  )
}

function PauseTenant({ tenants, onDone }: { tenants: string[]; onDone: () => void }) {
  const [tenant, setTenant] = useState(tenants[0] ?? '')
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  if (tenants.length === 0) return null
  const send = async () => {
    setBusy(true)
    setRefused(null)
    const r = await adminPauseTenantSchedules(tenant, tenant)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'empty') {
      setOpen(false)
      onDone()
    } else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <div className="as-tenant">
      <label className="c-field">
        <span className="c-lbl">Pause every enabled schedule of one workspace</span>
        <select className="c-inp" value={tenant} onChange={(e) => setTenant(e.target.value)}>
          {tenants.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>
      <Button kind="danger" onClick={() => { setOpen(true); setRefused(null) }}>
        Pause all of {tenant}’s schedules…
      </Button>
      {open && (
        <TypedConfirm
          title={`Pause all of ${tenant}’s schedules?`}
          name={tenant}
          verb="Pause them all"
          keep="Leave them running"
          busy={busy ? 'Pausing…' : false}
          onConfirm={() => void send()}
          onClose={() => setOpen(false)}
          error={refused === null ? undefined : `Not paused: ${refused.message}`}
        >
          <p>
            Every enabled schedule of {tenant} stops firing. Its members can resume each one. Live work is not cancelled.
          </p>
        </TypedConfirm>
      )}
    </div>
  )
}

/** The tenant this console works in (`GET /v1/tenants/me`), or null until it is read. */
function useWorkingTenant(): string | null {
  const [tenant, setTenant] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setTenant(r.data.tenant.tenant_id)
    })
    return () => {
      live = false
    }
  }, [])
  return tenant
}

function Rows({ rows, onChanged, go }: { rows: AdminScheduleRow[]; onChanged: () => void; go: (to: string) => void }) {
  const working = useWorkingTenant()
  const now = useNow()
  const [acting, setActing] = useState<{ row: AdminScheduleRow; action: Action } | null>(null)
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-admin">
        <thead>
          <tr>
            <th scope="col" data-col="tenant">Workspace</th>
            <th scope="col" data-col="name">Name · type</th>
            <th scope="col" data-col="when">When</th>
            <th scope="col" data-col="next">Next run</th>
            <th scope="col" data-col="last">Last outcome</th>
            <th scope="col" data-col="spend">Spend today</th>
            <th scope="col" data-col="approvals">Approvals</th>
            <th scope="col" data-col="state">State</th>
            <th scope="col" data-col="actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.schedule_id} className="sc-row" data-schedule={s.schedule_id}>
              <td data-label="Workspace"><span className="mono">{s.tenant_id}</span></td>
              <td data-label="Name · type">
                {s.tenant_id === working ? (
                  <InApp go={go} to={scheduleAddress(s.schedule_id)} className="sc-cut sc-name">{s.name}</InApp>
                ) : (
                  <span
                    className="sc-cut sc-name"
                    title={`${s.schedule_id} · switch the console to workspace ${s.tenant_id} to open this schedule's page`}
                  >
                    {s.name}
                  </span>
                )}
                <span className="sc-sub">{s.tier === null ? s.type : `${s.type} · ${s.tier}`}</span>
              </td>
              <td data-label="When">
                <span className="sc-cut" title={`${s.cron} · ${s.timezone}`}>{s.words ?? s.cron}</span>
                <span className="sc-sub">{s.timezone}</span>
              </td>
              <td data-label="Next run">
                {s.next_run_at === null ? (
                  <i className="ctl-em">&mdash; none while {STATE_WORD[s.state]}</i>
                ) : (
                  <span title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</span>
                )}
              </td>
              <td data-label="Last outcome">
                {s.last_outcome === null && s.last_firing_at === null ? (
                  <i className="ctl-em">never fired</i>
                ) : (
                  <>
                    <span>{s.last_outcome ?? 'in progress'}</span>
                    {s.last_firing_at !== null && <span className="sc-sub" title={s.last_firing_at}>{timeAgo(s.last_firing_at, now)}</span>}
                  </>
                )}
              </td>
              <td data-label="Spend today">
                <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
              </td>
              <td data-label="Approvals">{s.pending_approvals > 0 ? `${s.pending_approvals} waiting` : 'none'}</td>
              <td data-label="State">
                <span className={`sc-state is-${s.state}`}>{STATE_WORD[s.state]}</span>
                {s.pause !== null && s.state !== 'enabled' && (s.pause.reason || s.pause.code) && (
                  <span className="sc-sub sc-cut" title={`${s.pause.by ?? ''} ${s.pause.reason || s.pause.code}`}>
                    {s.pause.reason || s.pause.code}
                  </span>
                )}
              </td>
              <td data-label="Actions">
                <span className="as-acts">
                  {actionsFor(s.state).map((a) => (
                    <Button key={a} size="sm" kind={a === 'enable' ? 'secondary' : 'ghost'} onClick={() => setActing({ row: s, action: a })}>
                      {ACTION_WORD[a]}
                    </Button>
                  ))}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {acting !== null && (
        <ActionDialog
          row={acting.row}
          action={acting.action}
          onClose={() => setActing(null)}
          onDone={() => {
            setActing(null)
            onChanged()
          }}
        />
      )}
    </div>
  )
}

function AdminSchedulesBody({
  first,
  onChanged,
  go,
  scopeSwitch,
}: {
  first: { schedules: AdminScheduleRow[]; next_page_token: string | null; tick: ScheduleTickHealth }
  onChanged: () => void
  go: (to: string) => void
  /** The page's "Mine | All tenants" switch, drawn by `Schedules.tsx`. */
  scopeSwitch: ReactNode
}) {
  const [more, setMore] = useState<AdminScheduleRow[]>([])
  const [token, setToken] = useState<string | null>(first.next_page_token)
  const [reading, setReading] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const rows = [...first.schedules, ...more]
  const tenants = [...new Set(rows.map((r) => r.tenant_id))].sort()
  const readMore = async () => {
    if (token === null) return
    setReading(true)
    setFailed(null)
    const r = await loadAdminSchedules(token)
    setReading(false)
    if (r.status === 'ok' || r.status === 'stale') {
      setMore((m) => [...m, ...r.data.schedules])
      setToken(r.data.next_page_token)
    } else if (r.status === 'error') setFailed(r.error.message)
  }
  return (
    <div className="sc-list">
      <div className="c-acts sc-acts">{scopeSwitch}</div>
      <h2 className="sc-h2">The schedule tick</h2>
      <TickHealth tick={first.tick} />
      <h2 className="sc-h2">Every workspace’s schedules</h2>
      {rows.length === 0 ? (
        <Absent kind="zero" heading="No schedules in any workspace" say="The admin list was read and no workspace has a schedule.">
          A workspace’s schedules are listed here once it makes one under Automate › Schedules.
        </Absent>
      ) : (
        <>
          <Rows rows={rows} onChanged={onChanged} go={go} />
          {token !== null && (
            <div className="c-acts sc-acts">
              <Button size="sm" busy={reading ? 'Reading…' : false} onClick={() => void readMore()}>
                Read more
              </Button>
            </div>
          )}
          {failed !== null && <p className="c-hint" role="alert">More not read: {failed}</p>}
          <PauseTenant tenants={tenants} onDone={onChanged} />
        </>
      )}
    </div>
  )
}

/**
 * The All tenants view of Automate › Schedules. Mounted by `SchedulesScreen`
 * only for a session that is an admin, or whose session read failed -- and
 * `GET /v1/admin/schedules` is `is_admin` on the server either way, so a
 * refusal is the API's answer, drawn as the Screen's failure.
 */
export function AllTenantsSchedules({ go, scopeSwitch }: { go: (to: string) => void; scopeSwitch: ReactNode }) {
  const [round, setRound] = useState(0)
  return (
    <Screen
      key={`all-tenants-schedules:${round}`}
      title="Schedules"
      load={() => loadAdminSchedules()}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')}${d.next_page_token === null ? '' : ' on the first page'} · every workspace`}
    >
      {(d) => <AdminSchedulesBody first={d} onChanged={() => setRound((n) => n + 1)} go={go} scopeSwitch={scopeSwitch} />}
    </Screen>
  )
}
