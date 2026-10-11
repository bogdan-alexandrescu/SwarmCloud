/**
 * ONE SCHEDULE (docs/schedules.md §6.2, detail variant A): a head of facts,
 * then tabs -- History first, because the run history is the reason to open
 * the page; then Budget, Gate, Settings and Audit, which are long and rarely
 * read. Every value is `GET /v1/schedules/{id}` as served, and the audit is
 * its own route, read when its tab is opened. Two things write: Edit, which
 * swaps the tabs for the form (ScheduleEdit.tsx), and the SD3 merge switch on
 * the Gate tab (§4.2). After either, the page reads the schedule again rather
 * than patching what it drew, so it shows the revision the server holds.
 *
 * A schedule that has never fired shows the `Absent` state, not "0 runs"
 * (§6.2): no firing is a fact about the schedule, and a zero would read as a
 * count of something that ran.
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  loadSchedule,
  loadScheduleAudit,
  loadScheduleTypes,
  setMergeMode,
  type FiringWork,
  type Schedule,
  type ScheduleAuditEntry,
  type ScheduleBudgetUse,
  type ScheduleFiring,
  type ScheduleTypeRow,
} from './api'
import { Button, Dialog, Tabs } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { usd } from './measure'
import { pluralise } from './types'
import { Absent, Mark } from './primitives'
import { EditSchedule } from './ScheduleEdit'
import { InApp, SCHEDULES_ADDRESS, STATE_WORD, Said, ScheduleActions, SpendFigure, localTime, untilWords, type ActNotice } from './Schedules'
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

/** A share of the bar, as a CSS width: of the day's figure, or of the whole when the parts pass it. */
function share(part: number, whole: number): string {
  return whole <= 0 ? '0%' : `${Math.max(0, Math.min(100, (part / whole) * 100)).toFixed(2)}%`
}

/**
 * THE BURN BAR (detail A's Budget, owner decision 2026-10-11): today's
 * `per_day_usd` in four parts -- reported, each unreported attempt counted at
 * the per-run cap, each live work item reserved at the per-run cap, and free
 * -- every one a field of `budget_use` as served (`routes/schedules.py::
 * _budget_use`). The console adds nothing up: a part the server did not
 * serve is not drawn, and the bar is not drawn at all without `budget_use`.
 * Hatching is the not-measured part (the unreported attempts), as the usage
 * bar's rule says. When the parts pass the day's figure the bar is drawn
 * against their total, with the day's figure marked, and says so.
 */
export function BurnBar({ use }: { use: ScheduleBudgetUse }) {
  const parts = use.reported_usd + use.unreported_usd + use.live_usd
  const over = parts > use.per_day_usd
  const whole = over ? parts : use.per_day_usd
  const legend: [string, string][] = [
    ['reported', `Reported ${usd(use.reported_usd)}`],
    [
      'unreported',
      use.unreported_attempts > 0
        ? `${pluralise(use.unreported_attempts, 'attempt')} unreported, counted at the ${usd(use.per_run_usd)} per-run cap: ${usd(use.unreported_usd)}`
        : 'No attempt unreported',
    ],
    [
      'live',
      use.live_items > 0
        ? `${pluralise(use.live_items, 'live run')} reserved at ${usd(use.per_run_usd)} each: ${usd(use.live_usd)}`
        : 'No live run reserved',
    ],
    ['free', `Free ${usd(use.free_usd)}`],
  ]
  return (
    <div className="sc-burn" data-testid="sc-burn">
      <div className="sc-burn-h">
        <b>
          {usd(use.free_usd)} free of {usd(use.per_day_usd)} today
        </b>
        {use.unreported_attempts > 0 && (
          <Mark kind="partial" say={`${usd(use.reported_usd)} is a floor: ${pluralise(use.unreported_attempts, 'attempt')} did not report a cost`} />
        )}
      </div>
      <div
        className={`sc-burn-bar${over ? ' is-over' : ''}`}
        role="img"
        aria-label={legend.map(([, words]) => words).join('; ')}
      >
        <i className="sc-seg is-reported" data-part="reported" style={{ width: share(use.reported_usd, whole) }} />
        <i className="sc-seg is-unreported" data-part="unreported" style={{ width: share(use.unreported_usd, whole) }} />
        <i className="sc-seg is-live" data-part="live" style={{ width: share(use.live_usd, whole) }} />
        <i className="sc-seg is-free" data-part="free" style={{ width: share(use.free_usd, whole) }} />
        {over && <i className="sc-burn-tick" style={{ left: share(use.per_day_usd, whole) }} title={`The day's figure, ${usd(use.per_day_usd)}`} />}
      </div>
      <ul className="sc-burn-key">
        {legend.map(([part, words]) => (
          <li key={part} data-part={part}>
            <i className={`sc-key is-${part}`} aria-hidden="true" />
            {words}
          </li>
        ))}
      </ul>
      <p className="sb-note">
        {over
          ? `What is spent and held passes today's ${usd(use.per_day_usd)}, so a new firing is skipped until the day ends.`
          : `A new firing needs what its runs would reserve free under today's ${usd(use.per_day_usd)}; ${usd(use.free_usd)} is free now.`}{' '}
        The day is {use.day}, in the schedule's own timezone.
      </p>
    </div>
  )
}

function Budget({ s }: { s: Schedule }) {
  return (
    <div className="sc-budget">
      {s.budget_use === undefined || s.budget_use === null ? (
        <p className="sb-note" data-testid="sc-burn-absent">
          {s.budget_use === null
            ? '— no burn bar: the budget has no per-run or per-day figure to count against.'
            : '— no burn bar: this read did not serve today’s reservation.'}
        </p>
      ) : (
        <BurnBar use={s.budget_use} />
      )}
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
    </div>
  )
}

/** What the merge switch knows of the type's floor: `auto` allowed, never, or not read. */
type Floor = 'auto' | 'never' | 'unknown' | 'reading'

function useMergeFloor(type: string): Floor {
  const [floor, setFloor] = useState<Floor>('reading')
  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (!live) return
      if (r.status !== 'ok' && r.status !== 'stale') return setFloor('unknown')
      // A type this account may not create is not in its catalogue; the
      // server still holds the floor, so the switch is offered and it decides.
      const row: ScheduleTypeRow | undefined = r.data.types.find((t) => t.name === type)
      setFloor(row === undefined ? 'unknown' : row.floor_gate.merge === 'auto' ? 'auto' : 'never')
    })
    return () => {
      live = false
    }
  }, [type])
  return floor
}

/** The server's refusal of the switch, in the words of the rule that refused it. */
function switchRefusal(e: ApiError): string {
  switch (e.code) {
    case 'second_person_required':
      return 'You last changed this schedule’s gate, scope or spec, so another member must make this switch.'
    case 'platform_admin_required':
      return 'This schedule reaches a platform repository, where only a platform admin may switch merging to automatic.'
    case 'merge_auto_not_allowed':
      return 'This type never merges unattended.'
    case 'schedule_changed':
      return 'The schedule was changed since this page read it. Close this and read it again.'
    default:
      return `Not switched: ${e.message}`
  }
}

/**
 * THE SD3 MERGE SWITCH (§4.2, owner decision 2026-10-08). `approve` to `auto`
 * is the one way a schedule merges unattended: never the create or edit form,
 * always this audited route, and never by the member who last changed the
 * gate, scope or spec. The console does not decide who may: it asks, and says
 * which rule refused. A one-person tenant cannot meet the second-person rule,
 * so the server answers 422 `confirmation_required` and the dialog asks for
 * the schedule's name typed instead (SD5's exception). Back to `approve` is a
 * tightening, open to any member, and needs no dialog.
 */
export function MergeSwitch({ s, onChanged }: { s: Schedule; onChanged: () => void }) {
  const floor = useMergeFloor(s.type)
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const merge = s.gate.merge

  const send = async (mode: 'approve' | 'auto', confirm?: string) => {
    setBusy(true)
    setRefused(null)
    const r = await setMergeMode(s.schedule_id, { mode, revision: s.revision, ...(confirm === undefined ? {} : { confirm }) })
    setBusy(false)
    if (r.status === 'ok') {
      setOpen(false)
      onChanged()
      return
    }
    if (r.status !== 'error') return
    if (r.error.code === 'confirmation_required' && typed === null) setTyped('')
    setRefused(r.error)
  }

  if (merge === 'auto') {
    return (
      <div className="sc-switch" data-testid="sc-merge-switch" data-mode="auto">
        <p className="sb-note">
          Merging is <b>automatic</b>: a green, reviewed pull request merges without a person. The hard stops below still hold it.
        </p>
        <Button busy={busy ? 'Switching…' : false} onClick={() => void send('approve')}>
          Approve each merge again
        </Button>
        {refused !== null && <p className="c-hint" role="alert">{switchRefusal(refused)}</p>}
      </div>
    )
  }
  if (merge !== 'approve') {
    return (
      <p className="sb-note" data-testid="sc-merge-switch" data-mode={merge ?? 'unset'}>
        This schedule does not merge. Edit its gate to approve each merge before it can be switched to automatic.
      </p>
    )
  }
  if (floor === 'reading') return <p className="sb-note" data-testid="sc-merge-switch">Reading whether this type may merge unattended…</p>
  if (floor === 'never') {
    return (
      <p className="sb-note" data-testid="sc-merge-switch" data-mode="approve">
        Each merge is approved by a person, and stays so: {s.type} never merges unattended.
      </p>
    )
  }
  const confirmOk = typed === null || typed.trim() === s.name.trim()
  return (
    <div className="sc-switch" data-testid="sc-merge-switch" data-mode="approve">
      <p className="sb-note">
        Each merge is approved by a person.
        {floor === 'unknown' && ' Whether this type may merge unattended was not read; the server decides when you ask.'}
      </p>
      <Button onClick={() => { setOpen(true); setRefused(null) }}>Merge automatically…</Button>
      {open && (
        <Dialog
          title="Merge automatically?"
          onClose={() => setOpen(false)}
          actions={
            <>
              <Button kind="ghost" onClick={() => setOpen(false)}>
                Keep approving each merge
              </Button>
              <Button
                kind="primary"
                busy={busy ? 'Switching…' : false}
                disabledReason={confirmOk ? undefined : 'Type the schedule’s name exactly.'}
                onClick={() => void send('auto', typed === null ? undefined : typed.trim())}
              >
                Switch to automatic merging
              </Button>
            </>
          }
        >
          <p>
            A pull request this schedule makes will merge once it is green and reviewed, without a person approving it. The hard stops
            still hold whatever this says: a plan naming workflow files is refused; IAM, terraform/bootstrap, CODEOWNERS and the
            repository’s protected paths are held for a second member to approve (for the owner, in a platform repository); and in a
            platform repository the frozen contract is held for the owner.
          </p>
          <p>
            The member who last changed this schedule’s gate, scope or spec cannot make this switch; in a platform repository only a
            platform admin can. The switch is written to the schedule’s audit.
          </p>
          {typed !== null && (
            <label className="c-field">
              <span className="c-lbl">
                You are the only person in this tenant, so no second person can make this switch. Type <code>{s.name}</code> to confirm.
              </span>
              <input className="c-inp is-mono" value={typed} autoComplete="off" spellCheck={false} onChange={(e) => setTyped(e.target.value)} />
            </label>
          )}
          {refused !== null && !(refused.code === 'confirmation_required' && typed !== null) && (
            <p className="c-hint" role="alert">{switchRefusal(refused)}</p>
          )}
        </Dialog>
      )}
    </div>
  )
}

/** §4.4: the schedule's own `max_consecutive_failures`, clamped to 1-10, or 3, as `schedulefire.failures_to_pause` reads it. */
function failuresToPause(s: Schedule): number {
  const n = Number(s.policy.max_consecutive_failures ?? 3)
  return Number.isFinite(n) ? Math.min(10, Math.max(1, Math.trunc(n))) : 3
}

function Gate({ s, onChanged }: { s: Schedule; onChanged: () => void }) {
  const g = s.gate
  const failures = failuresToPause(s)
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
        <li>A plan naming .github/workflows/ is refused, not held: the credential cannot push it.</li>
        <li>
          IAM, terraform/bootstrap, CODEOWNERS and the repository’s own protected paths are held at plan and at merge for a second
          member to approve; in a platform repository, for the owner.
        </li>
        <li>In a platform repository, a change to the frozen contract is held for the owner.</li>
        <li>A security-class issue is not swept; if one is planned, its plan is held the same way and it never merges automatically.</li>
        <li>
          {failures === 1 ? 'A failed run pauses' : `${failures} failed runs in a row pause`} the schedule; a spent daily budget skips
          firings until the next day.
        </li>
      </ul>
      <h3 className="sc-h3">Merging</h3>
      <MergeSwitch s={s} onChanged={onChanged} />
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

function DetailBody({
  schedule: s,
  firings,
  go,
  tab,
  setTab,
  reread,
  notice,
  onDone,
}: {
  schedule: Schedule
  firings: ScheduleFiring[]
  go: (to: string) => void
  tab: DetailTab
  setTab: (t: DetailTab) => void
  reread: () => void
  notice: ActNotice | null
  onDone: (notice: ActNotice) => void
}) {
  const [editing, setEditing] = useState(false)
  const now = useNow()
  if (editing) {
    return (
      <EditSchedule
        schedule={s}
        onCancel={() => setEditing(false)}
        onSaved={() => {
          setEditing(false)
          reread()
        }}
      />
    )
  }
  return (
    <>
      <div className="sc-head-row">
        <ScheduleActions
          s={s}
          where="head"
          onDone={onDone}
          onDeleted={() => go(SCHEDULES_ADDRESS)}
        />
        <Button onClick={() => setEditing(true)}>Edit</Button>
      </div>
      <Said notice={notice} />
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
        {tab === 'gate' && <Gate s={s} onChanged={reread} />}
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
  // The tab lives here, above the Screen, so a re-read after a write -- a
  // new `round`, which remounts the Screen -- lands on the tab it came from.
  const [tab, setTab] = useState<DetailTab>('history')
  const [round, setRound] = useState(0)
  const [notice, setNotice] = useState<ActNotice | null>(null)
  return (
    <div className="sc-detail">
      <InApp go={go} to={SCHEDULES_ADDRESS} className="sc-back">‹ All schedules</InApp>
      <Screen
        key={`schedule:${scheduleId}:${round}`}
        title={pageTitle}
        load={async () => {
          const r = await loadSchedule(scheduleId)
          if (r.status === 'ok' || r.status === 'stale') setName(r.data.schedule.name)
          return r
        }}
        summary={(d) => `${d.schedule.schedule_id} · created by ${d.schedule.created_by}`}
      >
        {(d) => (
          <DetailBody
            schedule={d.schedule}
            firings={d.firings}
            go={go}
            tab={tab}
            setTab={setTab}
            reread={() => setRound((n) => n + 1)}
            notice={notice}
            onDone={(n) => {
              setNotice(n)
              setRound((r) => r + 1)
            }}
          />
        )}
      </Screen>
    </div>
  )
}
