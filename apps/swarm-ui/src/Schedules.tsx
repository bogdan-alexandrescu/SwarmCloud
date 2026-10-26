/**
 * AUTOMATE › SCHEDULES (docs/schedules.md §6, lane S7): the tenant's
 * schedules, read-only, beside the recurring work the platform runs for it on
 * its own (the index's cadence, §8.3), and Overview's "Waiting on you" card.
 *
 * WHAT A ROW CAN AND CANNOT SAY. Every figure is a field of
 * `routes/schedules.py::schedule_to_api`; nothing is derived from the cron
 * here, because the tick's own parser already wrote `words` and
 * `next_run_at` and a second parser in the browser is a second answer. A
 * schedule that is not enabled has no next run (`next_run_at` is null by
 * §1.1), and the cell says why rather than drawing a dash with no reason. A
 * spend some attempt never reported is a floor, so it carries the partial
 * mark and the count it is missing, never the floor alone (§6.2).
 *
 * A schedule is not a run. What a firing creates is ordinary work -- an issue
 * run, a task, a workflow -- and is listed in Work like any other; this
 * screen lists the thing that makes it, and links each firing to its work.
 */
import { Fragment, useEffect, useState, type MouseEvent, type ReactNode } from 'react'
import { AllTenantsSchedules } from './AdminSchedules'
import {
  deleteSchedule,
  loadApprovals,
  loadMe,
  loadRepositories,
  loadSchedules,
  pauseSchedule,
  resumeSchedule,
  runSchedule,
  type ApprovalItem,
  type Schedule,
  type ScheduleFiring,
} from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { usd } from './measure'
import { addressToPath } from './paths'
import { Absent, Mark } from './primitives'
import { scheduleWords, type RepoRecord } from './RepositoriesData'
import { Button, Dialog, Segmented, buttonClass } from './components'
import { ScheduleDetail, detailTabOf } from './ScheduleDetail'
import { NewSchedule } from './ScheduleEdit'
import { useRoleGate } from './Profile'
import { PageHead, Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

/**
 * The list's address, and one schedule's (`/automate/schedules/<id>`,
 * paths.ts). A tab other than History rides as `tab=`
 * (`/automate/schedules/<id>?tab=gate`) and the editor as `edit=1`
 * (`/automate/schedules/<id>/edit`), so a reload or a link lands there (AUT-11).
 */
export const SCHEDULES_ADDRESS = 'automate/schedules'
export function scheduleAddress(id: string, at: { tab?: string; edit?: boolean } = {}): string {
  const q = new URLSearchParams({ schedule: id })
  if (at.tab !== undefined && at.tab !== 'history') q.set('tab', at.tab)
  if (at.edit === true) q.set('edit', '1')
  return `${SCHEDULES_ADDRESS}?${q.toString()}`
}

/**
 * The create form's address: `/automate/schedules/new`. A schedule id is `sch_`
 * and twelve hex digits (`schedules.new_schedule_id`), so `new` names none.
 */
export const NEW_SCHEDULE = 'new'

/**
 * THE LIST'S TWO SCOPES (SCHED-ONE-PAGE, owner decision 2026-10-13,
 * docs/schedules.md §6.1). Schedules are one page: the tenant's own, and for
 * an admin every tenant's (`AllTenantsSchedules`, what Admin › Schedules was).
 * The scope is in the address, `?scope=all`, so an admin can link it;
 * `/admin/schedules` lands there (paths.ts).
 */
export const ALL_TENANTS_SCHEDULES = `${SCHEDULES_ADDRESS}?scope=all`
type Scope = 'mine' | 'all'

function scopeOf(view: string | null): Scope {
  return view !== null && new URLSearchParams(view).get('scope') === 'all' ? 'all' : 'mine'
}

/**
 * WHETHER THE SESSION IS AN ADMIN: `principal.is_admin` from `GET
 * /v1/tenants/me`, the read the nav's `admin: true` tabs and the header's
 * admin badge are drawn from, so the switch and the rest of the console agree
 * on who is an admin. `null` while it is read; `unknown` when the read failed.
 * It decides only what is DRAWN: `GET /v1/admin/schedules` is `is_admin` on
 * the server, whatever this says.
 */
function useSessionAdmin(): boolean | null | 'unknown' {
  const [admin, setAdmin] = useState<boolean | null | 'unknown'>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then((r) => {
      if (!live) return
      setAdmin(r.status === 'ok' || r.status === 'stale' ? r.data.principal.is_admin === true : 'unknown')
    })
    return () => {
      live = false
    }
  }, [])
  return admin
}

/**
 * "Mine | All workspaces" (BGD on #2157, 2026-10-23: Automate › Schedules keeps
 * its "All workspaces" switch; VQ-030): links, so either side opens in a new tab
 * and reads as where you are. The console says workspace, never tenant.
 */
function ScopeSwitch({ scope, go }: { scope: Scope; go: (to: string) => void }) {
  return (
    <Segmented<Scope>
      label="Whose schedules"
      small
      value={scope}
      options={[
        { key: 'mine', label: 'Mine', href: addressToPath(SCHEDULES_ADDRESS), title: 'This workspace’s schedules' },
        { key: 'all', label: 'All workspaces', href: addressToPath(ALL_TENANTS_SCHEDULES), title: 'Every workspace’s schedules and the schedule tick (admin)' },
      ]}
      onChange={(k) => go(k === 'all' ? ALL_TENANTS_SCHEDULES : SCHEDULES_ADDRESS)}
    />
  )
}

/** The schedule a Schedules address names (`schedule=<id>`), or null for the list. */
function scheduleOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('schedule')
  return id === null || id === '' ? null : id
}

/** A dry-run schedule is not a real one, and says so wherever its state is drawn (AUT-3). */
export function DryRunChip({ s }: { s: Pick<Schedule, 'policy'> }) {
  if (s.policy.dry_run !== true) return null
  return (
    <span className="sc-dry" data-testid="sc-dry" title="Each firing records what it would do and creates nothing. Turn dry run off under Edit.">
      dry run
    </span>
  )
}

/** A link inside the app: an href for a new tab, `go` for a plain click. */
export function InApp({
  to,
  go,
  children,
  className,
  current = false,
}: {
  to: string
  go: (to: string) => void
  children: ReactNode
  className?: string
  /** The link to the item this page has open: `aria-current`, so the choice is not colour alone. */
  current?: boolean
}) {
  return (
    <a
      href={addressToPath(to)}
      className={className}
      aria-current={current ? 'true' : undefined}
      onClick={(e: MouseEvent) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        go(to)
      }}
    >
      {children}
    </a>
  )
}

/** §1.3's states, as the person reads them. */
export const STATE_WORD: Readonly<Record<Schedule['state'], string>> = {
  enabled: 'enabled',
  paused: 'paused',
  auto_paused: 'auto-paused',
  disabled: 'disabled',
}

/** Why a schedule that is not enabled has no next run (§1.1: `next_run_at` is null). */
const NO_NEXT_RUN: Readonly<Record<Schedule['state'], string>> = {
  enabled: 'not computed: the tick has not set the next slot',
  paused: 'paused, so no next run',
  auto_paused: 'auto-paused, so no next run',
  disabled: 'disabled, so no next run',
}

/** "in 3h", "in 2d", or "due now" for a slot already past. Whole in the title. */
export function untilWords(iso: string, now: number): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return 'at an unknown time'
  const s = Math.round((t - now) / 1000)
  if (s <= 0) return 'due now'
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))}m`
  if (s < 48 * 3600) return `in ${Math.round(s / 3600)}h`
  return `in ${Math.round(s / 86400)}d`
}

/** The local time a timestamp names, for a title. */
export function localTime(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? `${d.toLocaleString()} (your time) · ${iso}` : iso
}

/**
 * A spend, with what it does not cover (§4.3). `unreported` attempts are not
 * zero: the figure is a floor, and the mark says so.
 */
export function SpendFigure({ usdReported, unreported, what }: { usdReported: number; unreported: number; what: string }) {
  if (unreported > 0) {
    const missing = pluralise(unreported, 'attempt')
    return (
      <span className="sc-spend">
        <Mark kind="partial" say={`${usd(usdReported)} is a floor: ${missing} of ${what} did not report a cost`} />{' '}
        <span className="sc-usd">{usd(usdReported)}</span> · {missing} unreported
      </span>
    )
  }
  return <span className="sc-spend"><span className="sc-usd">{usd(usdReported)}</span></span>
}

/**
 * A repository or a zone that breaks after its `/` first (visual QA V167):
 * "example-org/" then "example-api", never "example-org/example" then "-api".
 * Each piece is an inline block (`.sc-part`), so a line ends between pieces
 * before it ends inside one; a piece wider than its column still wraps. The
 * text and its copy are unchanged.
 */
export function slashBreaks(text: string): ReactNode {
  const parts = text.split('/')
  if (parts.length === 1) return text
  return parts.map((part, i) => (
    <Fragment key={i}>
      <span className="sc-part">{i < parts.length - 1 ? `${part}/` : part}</span>
      {i < parts.length - 1 && <wbr />}
    </Fragment>
  ))
}

/** The cron in words, or the expression itself when the parser refused it. */
function WhenCell({ s }: { s: Schedule }) {
  return (
    <>
      <span className="sc-cut" title={`${s.cron} · ${s.timezone}`}>{s.words ?? s.cron}</span>
      <span className="sc-sub sc-zone">{slashBreaks(s.timezone)}</span>
    </>
  )
}

/** The repositories a scope names, by `owner/repo` when the read gave them. */
function scopeWords(s: Schedule, names: ReadonlyMap<string, string> | null): string {
  if (s.scope.mode === 'all') return 'every registered repository'
  if (s.scope.mode === 'platform') return 'the platform'
  const ids = s.scope.repo_ids ?? []
  if (ids.length === 0) return 'no repository'
  const first = names?.get(ids[0]!) ?? ids[0]!
  return ids.length === 1 ? first : `${first} +${ids.length - 1}`
}

function stateLine(s: Schedule): string | null {
  if (s.pause === null || s.state === 'enabled') return null
  const by = s.pause.by ? ` by ${s.pause.by}` : ''
  const why = s.pause.reason || s.pause.code || null
  return why === null ? `Paused${by}` : `Paused${by}: ${why}`
}

// ---------------------------------------------------------------------------
// The actions (owner decisions 2026-10-11): Run now, Dry run, Pause, Resume
// and Delete, through the §7.1 routes as they are.
// ---------------------------------------------------------------------------

/** What an action said, kept above the page's read so a re-read does not drop it. */
export interface ActNotice {
  tone: 'ok' | 'bad'
  text: string
}

/**
 * WHETHER A RUN MADE NOW SPENDS CAPACITY. Only then does Run now ask first
 * (owner decision 2026-10-11: an action confirms only when it spends
 * capacity). A dry run runs no agent (§2.8); a schedule whose policy is dry
 * makes only dry firings; and under a `run: approve` gate the firing waits
 * for a person, holding nothing (invariant 1). Every other run creates work
 * at once, which admission leases like any other.
 */
export function runSpends(s: Schedule, dryRun: boolean): boolean {
  return !dryRun && s.policy.dry_run !== true && s.gate.run !== 'approve'
}

/** What a `:run` answer's firing came to, in words; nothing is assumed past the firing as written. */
export function firedWords(f: ScheduleFiring, dryRun: boolean): string {
  if (f.skip !== null) {
    if (f.skip.code === 'DRY_RUN') return `${dryRun ? 'Dry run' : 'Run'} recorded: nothing was created. What it would create is in History.`
    if (f.skip.code === 'NOTHING_STARTED') return 'Fired: nothing started. Why is on its row in History.'
    return `Not run: the firing was skipped, ${f.skip.code}.`
  }
  if (f.state === 'awaiting_approval') return 'Fired: the firing waits for approval in Approvals, holding no capacity.'
  if (f.state === 'queued') return 'Fired: the firing is queued behind the live one (the overlap policy).'
  if (f.work.length > 0) return `Fired: the firing created ${pluralise(f.work.length, 'work item')}.`
  return `Fired: the firing is ${f.state.replace(/_/g, ' ')}.`
}

/** The server's refusal of an action, in the words of the rule that refused it. */
export function actRefusal(e: ApiError): string {
  switch (e.code) {
    case 'schedule_disabled':
      return 'An admin disabled this schedule, so it neither runs nor resumes until an admin enables it.'
    case 'schedules_disabled':
      return 'Schedules are switched off platform-wide, so nothing runs now.'
    case 'admin_required':
      return 'An admin disabled this schedule; only an admin may enable it again.'
    case 'confirmation_required':
      return 'The name typed is not the schedule’s name, so nothing was deleted.'
    default:
      return `${errorHeading(e)}: ${e.message}`
  }
}

/** The budget the confirmation names, from the schedule as served; a figure not set is left out. */
function runCost(s: Schedule): string {
  const parts: string[] = []
  if (s.budget.per_run_usd !== undefined) parts.push(`each run is reserved at ${usd(s.budget.per_run_usd)} until it reports its cost`)
  if (s.budget.max_concurrent !== undefined) parts.push(`at most ${pluralise(s.budget.max_concurrent, 'live run')}`)
  if (s.budget.per_day_usd !== undefined) parts.push(`against ${usd(s.budget.per_day_usd)} a day`)
  return parts.length === 0 ? '' : ` Its budget holds: ${parts.join(', ')}.`
}

type ActKind = 'run' | 'dry' | 'pause' | 'resume' | 'delete'

/**
 * A SCHEDULE'S ACTIONS. On a row: Run now, and Resume when it is paused or
 * auto-paused. On its page: Run now, Dry run, Pause, Resume and Delete. Only
 * Run now asks first, and only when `runSpends`; Delete always asks for the
 * schedule's name typed, which the route checks again (`confirm`). Each
 * answer is said, and the page reads the schedule again rather than patching
 * what it drew. A disabled schedule is an admin's to enable: Run now says so
 * and Resume is not offered (`:resume` is 403 `admin_required` for it).
 */
export function ScheduleActions({
  s,
  where,
  onDone,
  onDeleted,
  foldId,
}: {
  s: Schedule
  where: 'row' | 'head'
  onDone: (notice: ActNotice) => void
  onDeleted?: (notice: ActNotice) => void
  /**
   * The head's id for what folds into More on a phone (VQ-029): every action
   * but Run now. A desktop draws them in the row as before (`.sc-fold` is
   * `display: contents`); the page's More button names this id.
   */
  foldId?: string
}) {
  const [busy, setBusy] = useState<ActKind | null>(null)
  const [asking, setAsking] = useState<'run' | 'delete' | null>(null)
  const [typed, setTyped] = useState('')
  const [refused, setRefused] = useState<string | null>(null)
  const size = where === 'row' ? 'sm' : 'md'
  // A VIEWER CHANGES NO SCHEDULE (docs/design/workspace-roles.md §4.8): every
  // write in routes/schedules.py needs `member`. The server still decides.
  const roleGate = useRoleGate('member', 'run and change schedules')
  const disabled = roleGate ?? (s.state === 'disabled' ? 'An admin disabled this schedule.' : undefined)

  const act = async (kind: ActKind) => {
    setBusy(kind)
    setRefused(null)
    if (kind === 'run' || kind === 'dry') {
      const r = await runSchedule(s.schedule_id, { dry_run: kind === 'dry' })
      setBusy(null)
      setAsking(null)
      if (r.status === 'ok') return onDone({ tone: 'ok', text: `${s.name}: ${firedWords(r.data.firing, kind === 'dry')}` })
      if (r.status === 'error') return onDone({ tone: 'bad', text: `${s.name}: ${actRefusal(r.error)}` })
      return
    }
    if (kind === 'delete') {
      const r = await deleteSchedule(s.schedule_id, typed.trim())
      setBusy(null)
      if (r.status === 'ok') {
        setAsking(null)
        return onDeleted?.({ tone: 'ok', text: `Deleted ${s.name}. Its history and audit are kept; work it already made is not cancelled.` })
      }
      if (r.status === 'error') setRefused(actRefusal(r.error))
      return
    }
    const r = kind === 'pause' ? await pauseSchedule(s.schedule_id) : await resumeSchedule(s.schedule_id)
    setBusy(null)
    if (r.status === 'ok') {
      const said =
        kind === 'pause'
          ? 'Paused: no new firings. Work it already made carries on.'
          : r.data.schedule.next_run_at === null
            ? 'Resumed.'
            : `Resumed: next run ${untilWords(r.data.schedule.next_run_at, Date.now())}.`
      return onDone({ tone: 'ok', text: `${s.name}: ${said}` })
    }
    if (r.status === 'error') onDone({ tone: 'bad', text: `${s.name}: ${actRefusal(r.error)}` })
  }

  const runNow = () => (runSpends(s, false) ? setAsking('run') : void act('run'))
  const resumable = s.state === 'paused' || s.state === 'auto_paused'
  return (
    <div className={where === 'row' ? 'sc-row-acts' : 'c-acts sc-head-acts'} data-testid={`sc-acts-${where}`}>
      <Button size={size} kind={where === 'head' ? 'primary' : 'secondary'} busy={busy === 'run' ? 'Running…' : false} disabledReason={disabled} onClick={runNow}>
        Run now
      </Button>
      {where === 'head' ? (
        <span className="sc-fold" id={foldId}>
          <Button size={size} busy={busy === 'dry' ? 'Running dry…' : false} disabledReason={disabled} onClick={() => void act('dry')}>
            Dry run
          </Button>
          {s.state === 'enabled' && (
            <Button size={size} busy={busy === 'pause' ? 'Pausing…' : false} disabled={roleGate !== null} onClick={() => void act('pause')}>
              Pause
            </Button>
          )}
          {resumable && (
            <Button size={size} busy={busy === 'resume' ? 'Resuming…' : false} disabled={roleGate !== null} onClick={() => void act('resume')}>
              Resume
            </Button>
          )}
          <Button size={size} kind="danger" disabled={roleGate !== null} onClick={() => { setTyped(''); setRefused(null); setAsking('delete') }}>
            Delete…
          </Button>
        </span>
      ) : (
        resumable && (
          <Button size={size} busy={busy === 'resume' ? 'Resuming…' : false} disabled={roleGate !== null} onClick={() => void act('resume')}>
            Resume
          </Button>
        )
      )}
      {asking === 'run' && (
        <Dialog
          title={`Run ${s.name} now?`}
          onClose={() => setAsking(null)}
          actions={
            <>
              <Button kind="ghost" onClick={() => setAsking(null)}>
                Not now
              </Button>
              <Button kind="primary" busy={busy === 'run' ? 'Running…' : false} onClick={() => void act('run')}>
                Run now
              </Button>
            </>
          }
        >
          <p>
            This fires the schedule once, now, outside its timetable. Its firing creates work at once, which takes capacity like any other run.
            {runCost(s)}
          </p>
          <p>A dry run creates nothing and runs no agent.</p>
        </Dialog>
      )}
      {asking === 'delete' && (
        <Dialog
          title={`Delete ${s.name}?`}
          onClose={() => setAsking(null)}
          actions={
            <>
              <Button kind="ghost" onClick={() => setAsking(null)}>
                Keep it
              </Button>
              <Button
                kind="danger-filled"
                busy={busy === 'delete' ? 'Deleting…' : false}
                disabledReason={typed.trim() === s.name.trim() ? undefined : 'Type the schedule’s name exactly.'}
                onClick={() => void act('delete')}
              >
                Delete schedule
              </Button>
            </>
          }
        >
          <p>
            The schedule stops firing for good and cannot be brought back. Its history and audit are kept, and work it already made is ordinary
            work that is not cancelled.
          </p>
          <label className="c-field">
            <span className="c-lbl">
              Type <code>{s.name}</code> to confirm.
            </span>
            <input className="c-inp is-mono" value={typed} autoComplete="off" spellCheck={false} onChange={(e) => setTyped(e.target.value)} />
          </label>
          {refused !== null && <p className="c-hint" role="alert">{refused}</p>}
        </Dialog>
      )}
    </div>
  )
}

/** What the last action said: a status, or an alert when it was refused. */
export function Said({ notice }: { notice: ActNotice | null }) {
  if (notice === null) return null
  return (
    <p className={`sc-said is-${notice.tone}`} role={notice.tone === 'bad' ? 'alert' : 'status'} data-testid="sc-said">
      {notice.text}
    </p>
  )
}

/**
 * The second line of a row on a phone: type · repository · when, then last ·
 * next · spend · waiting. IT WRAPS, AND CARRIES NO TITLE (visual QA V173): cut
 * to one line, everything after the cadence -- the next run, the outcome, the
 * spend and what waits -- sat past the 400px edge, readable only in a
 * `title`, which a touch screen never opens.
 */
function PhoneLine({ s, names, now }: { s: Schedule; names: ReadonlyMap<string, string> | null; now: number }) {
  const last =
    s.last_firing === null
      ? 'never fired'
      : `last ${s.last_firing.outcome ?? 'in progress'}${
          (s.last_firing.ended_at ?? s.last_firing.slot) === null ? '' : ` ${timeAgo(s.last_firing.ended_at ?? s.last_firing.slot ?? '', now)}`
        }`
  const next = s.next_run_at === null ? `no next run (${STATE_WORD[s.state]})` : `next ${untilWords(s.next_run_at, now)}`
  const waiting = s.pending_approvals > 0 ? `${s.pending_approvals} waiting` : 'none waiting'
  // THE PHONE KEEPS THE ROW'S FACTS (visual QA run 6, overview-automate-005):
  // the risk tier the type cell carries, the zone the When cell carries, and
  // the pause note the state cell carries all lived in cells a phone hides.
  const type = s.tier === null ? s.type : `${s.type} · ${s.tier}`
  const head = [type, scopeWords(s, names), `${s.words ?? s.cron}, ${s.timezone}`, last, next].join(' · ')
  const why = stateLine(s)
  return (
    <span className="sc-line2">
      {why !== null && <span className="sc-line2-why">{why}</span>}
      {head} · <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" /> ·{' '}
      {waiting}
    </span>
  )
}

function ScheduleList({
  rows,
  names,
  go,
  onDone,
}: {
  rows: Schedule[]
  names: ReadonlyMap<string, string> | null
  go: (to: string) => void
  onDone: (notice: ActNotice) => void
}) {
  const now = useNow()
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-schedules">
        <thead>
          <tr>
            <th scope="col" data-col="name">Name · type</th>
            <th scope="col" data-col="repo">Repository</th>
            <th scope="col" data-col="when">When</th>
            <th scope="col" data-col="next">Next run</th>
            <th scope="col" data-col="last">Last run</th>
            <th scope="col" data-col="spend">Spend today</th>
            <th scope="col" data-col="approvals">Approvals</th>
            <th scope="col" data-col="state">State</th>
            <th scope="col" data-col="acts">
              <span className="sc-vh">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const line = stateLine(s)
            return (
              <tr key={s.schedule_id} className="sc-row" data-schedule={s.schedule_id}>
                <td data-label="Name · type" className="sc-c-name">
                  <InApp go={go} to={scheduleAddress(s.schedule_id)} className="sc-cut sc-name">{s.name}</InApp>
                  <span className="sc-sub sc-type">{s.tier === null ? s.type : `${s.type} · ${s.tier}`}</span>
                  <PhoneLine s={s} names={names} now={now} />
                </td>
                <td data-label="Repository" className="sc-c-more"><span className="sc-cut" title={scopeWords(s, names)}>{slashBreaks(scopeWords(s, names))}</span></td>
                <td data-label="When" className="sc-c-more"><WhenCell s={s} /></td>
                <td data-label="Next run" className="sc-c-more">
                  {s.next_run_at === null ? (
                    <i className="ctl-em" title={NO_NEXT_RUN[s.state]}>&mdash; {NO_NEXT_RUN[s.state]}</i>
                  ) : (
                    <span title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</span>
                  )}
                </td>
                <td data-label="Last run" className="sc-c-more">
                  {s.last_firing === null ? (
                    <i className="ctl-em">never fired</i>
                  ) : (
                    <>
                      <span>{s.last_firing.outcome ?? 'in progress'}</span>
                      {(s.last_firing.ended_at ?? s.last_firing.slot) !== null && (
                        <span className="sc-sub" title={s.last_firing.ended_at ?? s.last_firing.slot ?? ''}>
                          {timeAgo(s.last_firing.ended_at ?? s.last_firing.slot ?? '', now)}
                        </span>
                      )}
                    </>
                  )}
                </td>
                <td data-label="Spend today" className="sc-c-more">
                  <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
                </td>
                <td data-label="Approvals" className="sc-c-more">{s.pending_approvals > 0 ? `${s.pending_approvals} waiting` : 'none'}</td>
                <td data-label="State" className="sc-c-state">
                  <span className={`sc-state is-${s.state}`}>{STATE_WORD[s.state]}</span>
                  <DryRunChip s={s} />
                  {line !== null && <span className="sc-sub sc-why">{line}</span>}
                </td>
                <td data-label="Actions" className="sc-c-acts">
                  <ScheduleActions s={s} where="row" onDone={onDone} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** The index's cadence in §8.3's words: "24 h + on change, at most every 30 min". */
function indexCadence(r: RepoRecord): string | null {
  const words = scheduleWords(r.index)
  if (words === null) return null
  const gap = r.index.min_change_interval_minutes
  return gap !== null && r.index.on_change !== 'off' && r.index.paused !== true ? `${words}, at most every ${gap} min` : words
}

/**
 * THE BUILT-IN ROWS (§8.3, SD7): each registration's index cadence, read-only,
 * linking to the repository's Settings where it is changed. They are not
 * schedules and the tick never reads them; they are here so that every
 * recurring thing this tenant has is in one list.
 */
function BuiltInRows({ repos, go }: { repos: Result<RepoRecord[]>; go: (to: string) => void }) {
  const now = useNow()
  if (repos.status === 'loading') return <p className="sb-note">Reading the built-in rows…</p>
  if (repos.status === 'error') {
    return (
      <Absent kind="failed" heading="Built-in rows not read" say={`The repositories read failed: ${errorHeading(repos.error)}`}>
        The index cadence of each repository is shown here once the repositories read succeeds.
      </Absent>
    )
  }
  const list = repos.status === 'empty' ? [] : repos.data
  if (list.length === 0) {
    return <p className="sb-note">No repository is registered, so the index runs nothing on its own.</p>
  }
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-builtin">
        <thead>
          <tr>
            <th scope="col" data-col="name">Built in</th>
            <th scope="col" data-col="when">When</th>
            <th scope="col" data-col="next">Next run</th>
            <th scope="col" data-col="settings">Where it is set</th>
          </tr>
        </thead>
        <tbody>
          {list.map((r) => {
            const cadence = indexCadence(r)
            return (
              <tr key={r.repo_id} className="sc-row" data-builtin={r.repo_id}>
                <td data-label="Built in"><span className="sc-built">Index · {slashBreaks(`${r.owner}/${r.repo}`)}</span></td>
                <td data-label="When">
                  {cadence === null ? <i className="ctl-em" title="The registration did not serve its index interval.">&mdash; not served</i> : cadence}
                </td>
                <td data-label="Next run">
                  {r.index.next_run_at === null ? (
                    <i className="ctl-em" title="The registration did not serve a next run.">&mdash; not served</i>
                  ) : (
                    <span title={localTime(r.index.next_run_at)}>{untilWords(r.index.next_run_at, now)}</span>
                  )}
                </td>
                <td data-label="Where it is set">
                  <InApp go={go} to={`work/repositories?${new URLSearchParams({ repo: r.repo_id, tab: 'settings' }).toString()}`}>
                    Repository Settings
                  </InApp>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function SchedulesBody({
  rows,
  repos,
  go,
  notice,
  onDone,
  scopeSwitch,
}: {
  rows: Schedule[]
  /** Read with the list, so ⟳ re-reads the built-in rows too (AUT-19). */
  repos: Result<RepoRecord[]>
  go: (to: string) => void
  notice: ActNotice | null
  onDone: (notice: ActNotice) => void
  /** "Mine | All workspaces", for an admin; nothing for anyone else. */
  scopeSwitch: ReactNode
}) {
  const names = repos.status === 'ok' || repos.status === 'stale' ? new Map(repos.data.map((r) => [r.repo_id, `${r.owner}/${r.repo}`])) : null
  const roleGate = useRoleGate('member', 'create schedules')
  return (
    <div className="sc-list">
      <div className="c-acts sc-acts">
        {scopeSwitch}
        {roleGate === null ? (
          <InApp go={go} to={scheduleAddress(NEW_SCHEDULE)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
            New schedule
          </InApp>
        ) : (
          <Button kind="primary" size="sm" disabledReason={roleGate}>
            New schedule
          </Button>
        )}
      </div>
      <Said notice={notice} />
      {rows.length === 0 ? (
        <Absent kind="zero" heading="No schedules yet" say="This workspace has no schedules: the list was read and is empty.">
          Recurring work in this workspace, such as an issue sweep, an index refresh or a nightly observer, is listed here. Each firing makes ordinary work: an issue run, an agent or a workflow.
        </Absent>
      ) : (
        <ScheduleList rows={rows} names={names} go={go} onDone={onDone} />
      )}
      <h2 className="sc-h2">Built-in recurring work</h2>
      <BuiltInRows repos={repos} go={go} />
    </div>
  )
}

/**
 * The list and the repositories in one read, so the Screen's ⟳ refreshes
 * the built-in index rows as well as the schedules (AUT-19). The list's own
 * answer is the screen's: a failed repositories read is the built-in rows'
 * failure, drawn there, and never the page's.
 */
async function loadListAndRepos(): Promise<Result<{ schedules: Schedule[]; tenant_id: string; repos: Result<RepoRecord[]> }>> {
  const [list, repos] = await Promise.all([loadSchedules(), loadRepositories()])
  if (list.status !== 'ok' && list.status !== 'stale') return list
  return { ...list, data: { ...list.data, repos } }
}

export function SchedulesScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const scheduleId = scheduleOf(view)
  if (scheduleId === NEW_SCHEDULE) return <NewSchedule go={go} />
  if (scheduleId !== null) {
    const q = new URLSearchParams(view ?? '')
    return <ScheduleDetail scheduleId={scheduleId} go={go} tab={detailTabOf(q.get('tab'))} editing={q.get('edit') === '1'} />
  }
  return <ScheduleLists scope={scopeOf(view)} go={go} />
}

function ScheduleLists({ scope, go }: { scope: Scope; go: (to: string) => void }) {
  // WHICH LIST, AND WHETHER THE SWITCH IS DRAWN. Only an admin sees "Mine |
  // All tenants". A non-admin following a `?scope=all` link gets their own
  // list and no switch -- the admin read is never made for them. While the
  // session is read, a `?scope=all` page waits rather than reading the admin
  // list for someone who may not be one; if the session read fails, it reads
  // it anyway and the API decides. (A comment here, not above: the nav
  // heading test follows SchedulesScreen's final `return <X />` only when
  // nothing stands between it and the next declaration.)
  const admin = useSessionAdmin()
  const scopeSwitch = admin === true ? <ScopeSwitch scope={scope} go={go} /> : null
  if (scope === 'all' && admin === null) return <PageHead title="Schedules" />
  if (scope === 'all' && admin !== false) return <AllTenantsSchedules go={go} scopeSwitch={scopeSwitch} />
  return <ScheduleListScreen go={go} scopeSwitch={scopeSwitch} />
}

function ScheduleListScreen({ go, scopeSwitch }: { go: (to: string) => void; scopeSwitch: ReactNode }) {
  // Above the Screen, as on one schedule's page: an action bumps `round`,
  // which remounts the Screen to read the list again, and what it said stays.
  const [round, setRound] = useState(0)
  const [notice, setNotice] = useState<ActNotice | null>(null)
  const onDone = (n: ActNotice) => {
    setNotice(n)
    setRound((r) => r + 1)
  }
  return (
    <Screen
      key={`schedules:${round}`}
      title="Schedules"
      load={loadListAndRepos}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')} · workspace ${d.tenant_id}`}
    >
      {(d) => <SchedulesBody rows={d.schedules} repos={d.repos} go={go} notice={notice} onDone={onDone} scopeSwitch={scopeSwitch} />}
    </Screen>
  )
}

// ---------------------------------------------------------------------------
// Overview's "Waiting on you" (§6.1)
// ---------------------------------------------------------------------------

/** How often the card re-reads the inbox while Overview is open. */
const WAITING_POLL_MS = 60_000

const KIND_WORD: Readonly<Record<string, string>> = {
  plan: 'Plan',
  hold: 'Held plan',
  merge: 'Merge',
  run: 'Run',
  proposal: 'Proposal',
  spec: 'Spec',
}

/** Where an item is decided from today: its run's page, or its schedule's. */
function subjectAddress(item: ApprovalItem): string | null {
  const s = item.subject
  if (typeof s.run_id === 'string' && s.run_id !== '') return `work/runs?${new URLSearchParams({ run: s.run_id }).toString()}`
  if (typeof s.schedule_id === 'string' && s.schedule_id !== '') return scheduleAddress(s.schedule_id)
  return null
}

/**
 * WAITING ON YOU: drawn ONLY when the inbox holds something (§6.1). Nothing
 * while reading, nothing for a measured zero, and nothing for a failed read
 * -- the spine's Automate badge carries that failure as a dash with its
 * reason, so a card that would say only "could not read" is not repeated
 * here. A waiting item holds no capacity (invariant 1), and the card says so.
 */
export function WaitingOnYouCard({ go }: { go: (to: string) => void }) {
  const [items, setItems] = useState<ApprovalItem[] | null>(null)
  useEffect(() => {
    let live = true
    const read = () =>
      void loadApprovals().then((r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') setItems(r.data.approvals)
        else if (r.status === 'empty') setItems([])
        // A failure keeps what the last read showed: the badge says it failed.
      })
    read()
    const timer = setInterval(read, WAITING_POLL_MS)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [])
  const now = useNow()
  if (items === null || items.length === 0) return null
  const shown = items.slice(0, 5)
  return (
    <section className="ctl-card ov-card sc-waiting" aria-labelledby="sc-waiting-h">
      <div className="sc-waiting-h">
        <h2 id="sc-waiting-h">Waiting on you</h2>
        <span className="sk-badge sc-count" aria-label={`${items.length} waiting`}>{items.length}</span>
      </div>
      <ul className="sc-waiting-list">
        {shown.map((item) => {
          const to = subjectAddress(item)
          const word = KIND_WORD[item.kind] ?? item.kind
          return (
            <li key={item.approval_id} data-approval={item.approval_id}>
              <span className="sc-kind">{word}</span>
              {to === null ? (
                <span className="sc-cut" title={item.summary}>{item.summary}</span>
              ) : (
                <InApp go={go} to={to} className="sc-cut">{item.summary}</InApp>
              )}
              {item.requested_at !== null && <span className="sc-sub" title={item.requested_at}>{timeAgo(item.requested_at, now)}</span>}
            </li>
          )
        })}
      </ul>
      <p className="sb-note">
        {items.length > shown.length ? `The oldest ${shown.length} of ${items.length}. ` : ''}Waiting items hold no capacity.
      </p>
    </section>
  )
}
