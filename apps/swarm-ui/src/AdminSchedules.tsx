/**
 * ADMIN › SCHEDULES (docs/schedules.md §5.3, §6.1-§6.2, §8.4, lane S7): every
 * tenant's schedules in one table with a tenant column (§6.2 variant A),
 * because sorting by tier, spend or failures across tenants is the admin's
 * question; the schedule tick's last hour; and the platform's own ticks.
 *
 * WHAT AN ADMIN MAY DO HERE, AND WHAT NOT. Pause, disable and re-enable any
 * schedule, and pause all of one tenant's, typed with that tenant's id --
 * each audited as `admin:<email>` by the server. Not edit another tenant's
 * parameters, gate, scope or spec, and not approve its approvals: those are
 * the tenant's decisions, and an admin's console session is not that
 * tenant's identity (§5.3). So the row carries no link into the tenant's own
 * schedule page, and `GET /v1/admin/schedules` serves none of those fields.
 *
 * A pause never cancels a tenant's live runs from here: that is the tenant's
 * `:pause` with `cancel_live`, or an admin's ordinary cancel of the work.
 */
import { useState } from 'react'
import {
  adminPauseTenantSchedules,
  adminScheduleAction,
  loadAdminSchedules,
  type AdminScheduleRow,
  type ScheduleTickHealth,
} from './api'
import { Button, Dialog, TypedConfirm } from './components'
import { type ApiError } from './fetch'
import { Absent } from './primitives'
import { STATE_WORD, SpendFigure, localTime, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

/**
 * §8.4: the platform's machinery, which no tenant manages and which stays in
 * Terraform (`terraform/modules/scheduler/jobs.tf`). A static list, so an
 * admin sees every recurring thing in one place; what each does is its Cloud
 * Scheduler job's own description. Nothing here is read from GCP.
 *
 * EVERY JOB, IN jobs.tf's ORDER (live QA ADM-6, 2026-10-11): the list said
 * "every recurring thing" and held 7 of the 12, schedule_tick among the
 * missing though its own report heads this page. `name` is the job's
 * resource name in jobs.tf and `job` its name after the prefix, `<tenant>`
 * for a per-tenant job; `adminSchedules.test.tsx` reads jobs.tf and fails
 * when the two disagree.
 */
export const PLATFORM_TICKS: readonly { name: string; job: string; does: string }[] = [
  { name: 'safety_tick', job: 'scheduler-tick', does: 'A one-minute safety tick for the scheduler’s drain loop.' },
  { name: 'reconciler', job: 'reconciler-tick', does: 'Reclaims expired leases and garbage-collects unused Job resources.' },
  { name: 'quota_refresh', job: 'quota-refresh', does: 'Recomputes provider quota state and adaptive pool targets.' },
  { name: 'workflow_rollup', job: 'workflow-rollup-<tenant>', does: 'Converges the stored state of each tenant’s workflows nobody reads.' },
  { name: 'issue_run_advance', job: 'issue-run-advance-<tenant>', does: 'Advances each tenant’s issue runs nobody is reading.' },
  { name: 'issue_sweep', job: 'issue-sweep-<tenant>', does: 'Starts issue runs for each tenant’s ready-looking open issues, up to its live-run cap.' },
  { name: 'stranded_pr_sweep', job: 'stranded-pr-sweep-<tenant>', does: 'Finds each tenant’s SwarmCloud pull requests nothing is going to merge, and logs them.' },
  { name: 'repo_index_poll', job: 'repo-index-poll-<tenant>', does: 'Reads each tenant’s registered repositories’ heads and queues their index runs.' },
  { name: 'merge_wake', job: 'merge-wake-<tenant>', does: 'Every minute, marks each tenant’s CI-waiting merge steps whose checks have settled.' },
  { name: 'forge_refresh', job: 'forge-refresh', does: 'Refreshes GitHub user access tokens before they expire.' },
  { name: 'schedule_tick', job: 'schedule-tick', does: 'Fires every tenant’s due schedules, one firing per slot (the tick reported above).' },
  { name: 'workspace_sweep', job: 'workspace-sweep', does: 'Re-dispatches approved personal workspaces nobody built, and logs each one still waiting.' },
]

type Action = 'pause' | 'disable' | 'enable'

const ACTION_WORD: Readonly<Record<Action, string>> = { pause: 'Pause', disable: 'Disable', enable: 'Re-enable' }

const ACTION_SAYS: Readonly<Record<Action, string>> = {
  pause: 'It stops firing until its tenant resumes it. Live work is not cancelled.',
  disable: 'It stops firing, and only an admin can enable it again; its tenant cannot resume it. Live work is not cancelled.',
  enable: 'It fires again from its next slot. Only a disabled schedule is re-enabled here: a tenant’s own pause is the tenant’s to lift.',
}

/** The actions a row offers in its state (§1.3, §5.3). */
function actionsFor(state: AdminScheduleRow['state']): Action[] {
  if (state === 'disabled') return ['enable']
  if (state === 'paused') return ['disable']
  return ['pause', 'disable']
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
      {tick.max_lateness_seconds === null ? <i className="ctl-em">&mdash; none measured</i> : `${tick.max_lateness_seconds} s at most`}
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
          <span className="c-lbl">Reason (shown to the tenant)</span>
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
        <span className="c-lbl">Pause every enabled schedule of one tenant</span>
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

function Rows({ rows, onChanged }: { rows: AdminScheduleRow[]; onChanged: () => void }) {
  const now = useNow()
  const [acting, setActing] = useState<{ row: AdminScheduleRow; action: Action } | null>(null)
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-admin">
        <thead>
          <tr>
            <th scope="col" data-col="tenant">Tenant</th>
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
              <td data-label="Tenant"><span className="mono">{s.tenant_id}</span></td>
              <td data-label="Name · type">
                <span className="sc-cut sc-name" title={s.schedule_id}>{s.name}</span>
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

function PlatformTicks() {
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-ticks" data-testid="as-platform-ticks">
        <thead>
          <tr>
            <th scope="col" data-col="name">Tick</th>
            <th scope="col" data-col="job">Cloud Scheduler job</th>
            <th scope="col" data-col="does">What it does</th>
          </tr>
        </thead>
        <tbody>
          {PLATFORM_TICKS.map((t) => (
            <tr key={t.name} className="sc-row" data-tick={t.name}>
              <td data-label="Tick"><span className="mono">{t.name}</span></td>
              <td data-label="Cloud Scheduler job"><span className="mono">{t.job}</span></td>
              <td data-label="What it does">{t.does}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AdminSchedulesBody({
  first,
  onChanged,
}: {
  first: { schedules: AdminScheduleRow[]; next_page_token: string | null; tick: ScheduleTickHealth }
  onChanged: () => void
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
      <h2 className="sc-h2">The schedule tick</h2>
      <TickHealth tick={first.tick} />
      <h2 className="sc-h2">Every tenant’s schedules</h2>
      {rows.length === 0 ? (
        <Absent kind="zero" heading="No schedules in any tenant" say="The admin list was read and no tenant has a schedule.">
          A tenant’s schedules are listed here once it makes one under Automate › Schedules.
        </Absent>
      ) : (
        <>
          <Rows rows={rows} onChanged={onChanged} />
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
      <h2 className="sc-h2">Platform ticks</h2>
      <p className="sb-note">The platform’s own machinery. No tenant manages these; they are set in Terraform, and listed here so every recurring thing is in one place.</p>
      <PlatformTicks />
    </div>
  )
}

export function AdminSchedulesScreen() {
  const [round, setRound] = useState(0)
  return (
    <Screen
      key={`admin-schedules:${round}`}
      title="Schedules"
      load={() => loadAdminSchedules()}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')}${d.next_page_token === null ? '' : ' on the first page'} · every tenant`}
    >
      {(d) => <AdminSchedulesBody first={d} onChanged={() => setRound((n) => n + 1)} />}
    </Screen>
  )
}
