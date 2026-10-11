/**
 * ONE SCHEDULE (docs/schedules.md §6.2, detail variant A): a head of facts,
 * then tabs -- History first, because the run history is the reason to open
 * the page; then Budget, Gate, Settings and Audit, which are long and rarely
 * read. Read-only: every value is `GET /v1/schedules/{id}` as served, and the
 * audit is its own route, read when its tab is opened.
 *
 * A schedule that has never fired shows the `Absent` state, not "0 runs"
 * (§6.2): no firing is a fact about the schedule, and a zero would read as a
 * count of something that ran.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { loadSchedule, loadScheduleAudit, type FiringWork, type Schedule, type ScheduleAuditEntry, type ScheduleFiring } from './api'
import { Tabs } from './components'
import { errorHeading, type Result } from './fetch'
import { usd } from './measure'
import { Absent } from './primitives'
import { InApp, SCHEDULES_ADDRESS, STATE_WORD, SpendFigure, localTime, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { useNow } from './useNow'

type DetailTab = 'history' | 'budget' | 'gate' | 'settings' | 'audit'

/** Where one piece of a firing's work is read: Work's own page for it. */
function workAddress(w: FiringWork): string | null {
  if (w.kind === 'issue_run') return `work/runs?${new URLSearchParams({ run: w.id }).toString()}`
  if (w.kind === 'task') return `work/task/${encodeURIComponent(w.id)}`
  if (w.kind === 'workflow') return `work/workflows?${new URLSearchParams({ wf: w.id }).toString()}`
  // An api_action (a label, a comment) has no page of its own here.
  return null
}

/** What a firing came to: its outcome once ended, its state while it is not. */
function firingWord(f: ScheduleFiring): string {
  if (f.skip !== null) return `skipped: ${f.skip.code}`
  return f.outcome ?? f.state.replace(/_/g, ' ')
}

function FiringRows({ firings, go }: { firings: ScheduleFiring[]; go: (to: string) => void }) {
  const now = useNow()
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-firings">
        <thead>
          <tr>
            <th scope="col" data-col="slot">Fired</th>
            <th scope="col" data-col="trigger">Trigger</th>
            <th scope="col" data-col="outcome">Outcome</th>
            <th scope="col" data-col="work">Work</th>
            <th scope="col" data-col="cost">Cost</th>
          </tr>
        </thead>
        <tbody>
          {firings.map((f) => {
            const at = f.fired_at ?? f.slot
            return (
              <tr key={f.firing_id} className="sc-row" data-firing={f.firing_id}>
                <td data-label="Fired">
                  {at === null ? <i className="ctl-em">&mdash; not recorded</i> : <span title={localTime(at)}>{timeAgo(at, now)}</span>}
                </td>
                <td data-label="Trigger">{f.trigger.replace(/_/g, ' ')}</td>
                <td data-label="Outcome">
                  <span className="sc-cut" title={firingWord(f)}>{firingWord(f)}</span>
                  {f.approval_id !== null && f.state === 'awaiting_approval' && <span className="sc-sub">waiting for approval, holding nothing</span>}
                </td>
                <td data-label="Work">
                  {f.work.length === 0 ? (
                    <i className="ctl-em">none created</i>
                  ) : (
                    <span className="sc-work">
                      {f.work.map((w) => {
                        const to = workAddress(w)
                        return to === null ? (
                          <span key={`${w.kind}:${w.id}`} className="mono">{w.id}</span>
                        ) : (
                          <InApp key={`${w.kind}:${w.id}`} go={go} to={to} className="mono">{w.id}</InApp>
                        )
                      })}
                    </span>
                  )}
                </td>
                <td data-label="Cost">
                  {f.cost === null ? (
                    <i className="ctl-em" title="No attempt of this firing has ended, or it created no work.">&mdash; none reported</i>
                  ) : (
                    <SpendFigure usdReported={f.cost.reported_usd ?? 0} unreported={f.cost.unreported_attempts ?? 0} what="this firing" />
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** One fact of the head: a name and its value. */
function Fact({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="sc-fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function money(v: number | undefined): string {
  return v === undefined ? '— not set' : usd(v)
}

function Budget({ s }: { s: Schedule }) {
  return (
    <dl className="sc-facts" data-testid="sc-budget">
      <Fact name="Per run">{money(s.budget.per_run_usd)}</Fact>
      <Fact name="Per day">{money(s.budget.per_day_usd)}</Fact>
      <Fact name="Live runs at most">{s.budget.max_concurrent ?? '— not set'}</Fact>
      <Fact name={`Spent ${s.spend_today.day}`}>
        <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
      </Fact>
      <p className="sb-note sc-wide">
        Cost is written when an attempt ends, so a run is not stopped at the per-run figure: a run that ends over it pauses this
        schedule, and a day can end above the daily figure by what its live runs overspend.
      </p>
    </dl>
  )
}

function Gate({ s }: { s: Schedule }) {
  const g = s.gate
  return (
    <div data-testid="sc-gate">
      <dl className="sc-facts">
        <Fact name="Tier">{s.tier ?? '— the type has left the catalogue'}</Fact>
        <Fact name="Run">{g.run ?? '— not set'}</Fact>
        <Fact name="Plan">{g.plan ?? '— not set'}</Fact>
        <Fact name="Merge">{g.merge ?? '— not set'}</Fact>
        <Fact name="Approvers">{g.approvers ?? '— not set'}</Fact>
        <Fact name="Expiry">{g.approval_ttl_hours === undefined ? '— not set' : `${g.approval_ttl_hours} h`}</Fact>
      </dl>
      <h3 className="sc-h3">Hard stops, whatever the gate</h3>
      <ul className="sc-stops">
        <li>A plan naming .github/workflows/ is refused: the credential cannot push it.</li>
        <li>IAM, terraform/bootstrap and CODEOWNERS changes are held for the owner, at plan and at merge.</li>
        <li>A security-class issue is not swept; if one is planned, its plan needs the owner.</li>
        <li>Three failed runs in a row pause the schedule; a spent daily budget skips firings until the next day.</li>
      </ul>
    </div>
  )
}

function Settings({ s }: { s: Schedule }) {
  const params = Object.entries(s.params)
  return (
    <dl className="sc-facts">
      {params.length === 0 ? (
        <Fact name="Parameters">the type's defaults</Fact>
      ) : (
        params.map(([k, v]) => (
          <Fact key={k} name={k}>
            <span className="mono">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
          </Fact>
        ))
      )}
      <Fact name="Overlap">{s.policy.overlap ?? '— not set'}</Fact>
      <Fact name="Catch-up">{s.policy.catch_up ?? '— not set'}</Fact>
      <Fact name="Jitter">{s.policy.jitter === undefined ? '— not set' : s.policy.jitter ? 'on' : 'off'}</Fact>
      <Fact name="Dry run">{s.policy.dry_run === undefined ? '— not set' : s.policy.dry_run ? 'on' : 'off'}</Fact>
      <Fact name="Revision">{s.revision}</Fact>
    </dl>
  )
}

function Audit({ scheduleId }: { scheduleId: string }) {
  const [r, setR] = useState<Result<{ audit: ScheduleAuditEntry[] }>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadScheduleAudit(scheduleId).then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [scheduleId])
  const now = useNow()
  if (r.status === 'loading') return <p className="sb-note">Reading the audit…</p>
  if (r.status === 'error') {
    return <Absent kind="failed" heading="Audit not read" say={`The audit read failed: ${errorHeading(r.error)}`} />
  }
  if (r.status === 'empty') {
    return <Absent kind="zero" heading="No audit entries" say="The audit was read and holds no entry for this schedule." />
  }
  return (
    <ol className="sc-audit">
      {r.data.audit.map((e, i) => {
        const detail = Object.entries(e.detail)
          .map(([k, v]) => `${k} ${typeof v === 'string' ? v : JSON.stringify(v)}`)
          .join(' · ')
        return (
          <li key={`${e.at}:${i}`} data-audit={e.action}>
            <span className="sc-who">{e.by}</span> <b>{e.action.replace(/_/g, ' ')}</b>
            {detail !== '' && <span className="sc-sub sc-adetail">{detail}</span>}
            <span className="sc-sub" title={localTime(e.at)}>{timeAgo(e.at, now)}</span>
          </li>
        )
      })}
    </ol>
  )
}

function DetailBody({ schedule: s, firings, go }: { schedule: Schedule; firings: ScheduleFiring[]; go: (to: string) => void }) {
  const [tab, setTab] = useState<DetailTab>('history')
  const now = useNow()
  return (
    <>
      <dl className="sc-facts sc-head">
        <Fact name="Type">{s.tier === null ? s.type : `${s.type} · ${s.tier}`}</Fact>
        <Fact name="When">
          {s.words ?? s.cron} <span className="sc-sub mono">{s.cron} · {s.timezone}</span>
        </Fact>
        <Fact name="Scope">
          {s.scope.mode === 'all' ? 'every registered repository' : s.scope.mode === 'platform' ? 'the platform' : (s.scope.repo_ids ?? []).join(', ')}
        </Fact>
        <Fact name="Owner">{s.owner}</Fact>
        <Fact name="State">
          {STATE_WORD[s.state]}
          {s.pause !== null && s.state !== 'enabled' && (s.pause.reason || s.pause.code) && <span className="sc-sub">{s.pause.reason || s.pause.code}</span>}
        </Fact>
        <Fact name="Next run">
          {s.next_run_at === null ? <i className="ctl-em">&mdash; none while {STATE_WORD[s.state]}</i> : <span title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</span>}
        </Fact>
        <Fact name="Spend today">
          <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
        </Fact>
        <Fact name="Waiting">{s.pending_approvals > 0 ? `${s.pending_approvals} for approval` : 'none'}</Fact>
      </dl>
      <Tabs
        className="sc-tabs"
        label="Views of this schedule"
        current={tab}
        onSelect={(k) => setTab(k as DetailTab)}
        tabs={[
          { key: 'history', label: 'History', count: firings.length },
          { key: 'budget', label: 'Budget' },
          { key: 'gate', label: 'Gate' },
          { key: 'settings', label: 'Settings' },
          { key: 'audit', label: 'Audit' },
        ]}
      />
      <div className="sc-pane" role="tabpanel">
        {tab === 'history' &&
          (firings.length === 0 ? (
            <Absent kind="zero" heading="Never fired" say="This schedule has no firing recorded: it has not fired yet." />
          ) : (
            <FiringRows firings={firings} go={go} />
          ))}
        {tab === 'budget' && <Budget s={s} />}
        {tab === 'gate' && <Gate s={s} />}
        {tab === 'settings' && <Settings s={s} />}
        {tab === 'audit' && <Audit scheduleId={s.schedule_id} />}
      </div>
    </>
  )
}

export function ScheduleDetail({ scheduleId, go }: { scheduleId: string; go: (to: string) => void }) {
  // A local, not a literal: the page is headed by the schedule's own name,
  // which the nav-heading test skips as a per-object title. Until the read
  // lands, the id is all this page knows to call it.
  const [name, setName] = useState<string | null>(null)
  const pageTitle = name ?? scheduleId
  return (
    <div className="sc-detail">
      <InApp go={go} to={SCHEDULES_ADDRESS} className="sc-back">‹ All schedules</InApp>
      <Screen
        key={`schedule:${scheduleId}`}
        title={pageTitle}
        load={async () => {
          const r = await loadSchedule(scheduleId)
          if (r.status === 'ok' || r.status === 'stale') setName(r.data.schedule.name)
          return r
        }}
        summary={(d) => `${d.schedule.schedule_id} · created by ${d.schedule.created_by}`}
      >
        {(d) => <DetailBody schedule={d.schedule} firings={d.firings} go={go} />}
      </Screen>
    </div>
  )
}
