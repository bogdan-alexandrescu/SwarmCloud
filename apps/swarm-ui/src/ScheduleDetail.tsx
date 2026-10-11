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
import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  loadMe,
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
import {
  DryRunChip,
  InApp,
  SCHEDULES_ADDRESS,
  STATE_WORD,
  Said,
  ScheduleActions,
  SpendFigure,
  localTime,
  scheduleAddress,
  untilWords,
  type ActNotice,
} from './Schedules'
import { Screen, timeAgo } from './Shell'
import { useNow } from './useNow'

export type DetailTab = 'history' | 'budget' | 'gate' | 'settings' | 'audit'

/** The tabs, in the order drawn; the first is the page's default and carries no `tab=`. */
export const DETAIL_TABS: readonly DetailTab[] = ['history', 'budget', 'gate', 'settings', 'audit']

/** A `tab=` value as a tab, or History for anything else (AUT-11). */
export function detailTabOf(v: string | null): DetailTab {
  return DETAIL_TABS.includes(v as DetailTab) ? (v as DetailTab) : 'history'
}

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
    case 'confirmation_required':
      return 'The name typed is not the schedule’s name, so merging was not switched.'
    default:
      return `Not switched: ${e.message}`
  }
}

/**
 * WHO LAST CHANGED THE GATE, SCOPE OR SPEC, as `schedaudit.last_editor` reads
 * it: the newest audit entry that is a create, a merge switch, or an edit
 * whose `changed` names `gate`, `scope` or `params`; with none, the creator.
 * An admin's `admin:<email>` is that email, and the tick is nobody. This is a
 * reading for the dialog's sake, never the rule: the server decides, and its
 * refusal is still said in place.
 */
export function lastGateEditor(audit: readonly ScheduleAuditEntry[], s: Pick<Schedule, 'created_by'>): string | null {
  const person = (by: unknown): string | null => {
    const text = String(by ?? '').trim().toLowerCase()
    return text === '' || text === 'schedule-tick' ? null : text.replace(/^admin:/, '')
  }
  for (const e of audit) {
    if (e.action === 'create' || e.action === 'gate_merge_auto' || e.action === 'gate_merge_approve') return person(e.by)
    if (e.action === 'edit') {
      const changed = (e.detail as { changed?: Record<string, unknown> }).changed ?? {}
      if (['gate', 'scope', 'params'].some((k) => k in changed)) return person(e.by)
    }
  }
  return person(s.created_by)
}

/**
 * Whether this viewer is the one the second-person rule refuses (AUT-4), or
 * null while that is not known. A personal tenant types the name instead, and
 * a platform scope is an admin's rule, so neither is blocked here.
 */
function useSwitchBlock(s: Schedule, ask: boolean): string | null {
  const [why, setWhy] = useState<string | null>(null)
  useEffect(() => {
    if (!ask) return
    let live = true
    void Promise.all([loadMe({ frame: true }), loadScheduleAudit(s.schedule_id)]).then(([me, audit]) => {
      if (!live || (me.status !== 'ok' && me.status !== 'stale')) return
      if (me.data.tenant.kind !== 'group' || s.scope.mode === 'platform') return setWhy(null)
      const entries = audit.status === 'ok' || audit.status === 'stale' ? audit.data.audit : audit.status === 'empty' ? [] : null
      if (entries === null) return
      const editor = lastGateEditor(entries, s)
      setWhy(
        editor !== null && editor === me.data.principal.email.trim().toLowerCase()
          ? 'You last changed this schedule’s gate, scope or spec, so another member of this tenant must make this switch.'
          : null,
      )
    })
    return () => {
      live = false
    }
  }, [ask, s])
  return why
}

/**
 * THE SD3 MERGE SWITCH (§4.2, owner decision 2026-10-08). `approve` to `auto`
 * is the one way a schedule merges unattended: never the create or edit form,
 * always this audited route, and never by the member who last changed the
 * gate, scope or spec. The server holds every rule and says which refused;
 * the dialog also reads who last changed the gate and disables Switch for
 * that member, with the reason beside it (AUT-4), rather than offering a
 * button the server will refuse.
 *
 * TWO STEPS (AUT-4). The first says what changes and what still holds; the
 * second asks for the schedule's name typed, and only then sends. The typed
 * name rides as `confirm`, which is what a one-person tenant's switch needs
 * (422 `confirmation_required`, SD5's exception) and which the route ignores
 * elsewhere. Back to `approve` is a tightening, open to any member, and needs
 * no dialog. Closing the dialog returns focus to its trigger (AUT-13).
 */
export function MergeSwitch({ s, onChanged }: { s: Schedule; onChanged: () => void }) {
  const floor = useMergeFloor(s.type)
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState<'explain' | 'type'>('explain')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const trigger = useRef<HTMLSpanElement | null>(null)
  const merge = s.gate.merge
  const blocked = useSwitchBlock(s, merge === 'approve' && floor !== 'never')

  const close = () => {
    setOpen(false)
    // The Dialog hands focus back to whatever held it when it opened; a
    // click that never focused the trigger left that on <body> (AUT-13).
    requestAnimationFrame(() => trigger.current?.querySelector('button')?.focus())
  }

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
    if (r.status === 'error') setRefused(r.error)
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
  const typedOk = typed.trim() === s.name.trim()
  return (
    <div className="sc-switch" data-testid="sc-merge-switch" data-mode="approve">
      <p className="sb-note">
        Each merge is approved by a person.
        {floor === 'unknown' && ' Whether this type may merge unattended was not read; the server decides when you ask.'}
      </p>
      <span ref={trigger}>
        <Button onClick={() => { setOpen(true); setStep('explain'); setTyped(''); setRefused(null) }}>Merge automatically…</Button>
      </span>
      {open && (
        <Dialog
          title={step === 'explain' ? 'Merge automatically?' : 'Confirm automatic merging'}
          onClose={close}
          actions={
            step === 'explain' ? (
              <>
                <Button kind="ghost" onClick={close}>
                  Keep approving each merge
                </Button>
                <Button kind="primary" disabledReason={blocked ?? undefined} onClick={() => { setStep('type'); setRefused(null) }}>
                  Switch to automatic merging…
                </Button>
              </>
            ) : (
              <>
                <Button kind="ghost" onClick={() => setStep('explain')}>
                  Back
                </Button>
                <Button
                  kind="primary"
                  busy={busy ? 'Switching…' : false}
                  disabledReason={blocked ?? (typedOk ? undefined : 'Type the schedule’s name exactly.')}
                  onClick={() => void send('auto', typed.trim())}
                >
                  Switch now
                </Button>
              </>
            )
          }
        >
          {step === 'explain' ? (
            <>
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
            </>
          ) : (
            <label className="c-field">
              <span className="c-lbl">
                Type <code>{s.name}</code> to switch merging to automatic. In a tenant of one person, this typed name stands in for the
                second person.
              </span>
              <input
                className="c-inp is-mono"
                value={typed}
                autoFocus
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setTyped(e.target.value)}
              />
            </label>
          )}
          {refused !== null && (
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
  editing,
  reread,
  notice,
  onDone,
}: {
  schedule: Schedule
  firings: ScheduleFiring[]
  go: (to: string) => void
  tab: DetailTab
  editing: boolean
  reread: () => void
  notice: ActNotice | null
  onDone: (notice: ActNotice) => void
}) {
  const now = useNow()
  // THE TAB AND THE EDITOR ARE IN THE ADDRESS (AUT-11): a reload, Back and a
  // pasted link land where the person was.
  const at = (next: { tab?: DetailTab; edit?: boolean }) => go(scheduleAddress(s.schedule_id, next))
  if (editing) {
    return (
      <EditSchedule
        schedule={s}
        onCancel={() => at({ tab })}
        onSaved={() => {
          at({ tab })
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
        <Button onClick={() => at({ tab, edit: true })}>Edit</Button>
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
          <DryRunChip s={s} />
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
        onSelect={(k) => at({ tab: k as DetailTab })}
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

export function ScheduleDetail({
  scheduleId,
  go,
  tab = 'history',
  editing = false,
}: {
  scheduleId: string
  go: (to: string) => void
  /** The tab the address names (`tab=`); a re-read after a write lands on it. */
  tab?: DetailTab
  /** The address names the editor (`/automate/schedules/<id>/edit`). */
  editing?: boolean
}) {
  // A local, not a literal: the page is headed by the schedule's own name,
  // which the nav-heading test skips as a per-object title. Until the read
  // lands, the id is all this page knows to call it.
  const [name, setName] = useState<string | null>(null)
  const pageTitle = name ?? scheduleId
  const [round, setRound] = useState(0)
  const [notice, setNotice] = useState<ActNotice | null>(null)
  // A 404 AFTER A READ THAT FOUND IT IS A DELETION (AUT-9), not a read
  // failure: the stale record with Edit enabled is not drawn over it.
  const seen = useRef(false)
  const [gone, setGone] = useState(false)
  if (gone) {
    return (
      <div className="sc-detail">
        <InApp go={go} to={SCHEDULES_ADDRESS} className="sc-back">‹ All schedules</InApp>
        <h1 className="sc-gone-h">{pageTitle}</h1>
        <Absent
          kind="zero"
          heading="This schedule was deleted"
          say="It was read here before, and the schedule is no longer there: someone deleted it."
        >
          Its firings and audit are kept, and work it made carries on as ordinary work.{' '}
          <InApp go={go} to={SCHEDULES_ADDRESS} className="ctl-link">
            Back to all schedules
          </InApp>
        </Absent>
      </div>
    )
  }
  return (
    <div className="sc-detail">
      <InApp go={go} to={SCHEDULES_ADDRESS} className="sc-back">‹ All schedules</InApp>
      <Screen
        key={`schedule:${scheduleId}:${round}`}
        title={pageTitle}
        load={async () => {
          const r = await loadSchedule(scheduleId)
          if (r.status === 'ok' || r.status === 'stale') {
            seen.current = true
            setName(r.data.schedule.name)
          } else if (r.status === 'error' && r.error.kind === 'not_found' && seen.current) {
            setGone(true)
          }
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
            editing={editing}
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
