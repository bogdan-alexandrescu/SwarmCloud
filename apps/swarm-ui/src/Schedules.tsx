/**
 * AUTOMATE › SCHEDULES (docs/schedules.md §6, lane S7): the tenant's
 * schedules, read-only, beside the recurring work the platform runs for it on
 * its own (the index's cadence, §8.3), and Overview's "Waiting on you" card.
 *
 * THE LANDING IS PROPOSAL A's FRAME A1 (BGD's picks, 2026-10-26, #2417): a
 * lead naming what needs the reader, the schedules with each one's served
 * `state_word`, and an approvals card counting every inbox item with the
 * oldest one's expiry. A row carries no actions: they are on the schedule's
 * own page, as the frame draws them.
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
import { Fragment, useEffect, useRef, useState, type ComponentProps, type MouseEvent, type ReactNode } from 'react'
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
import { Button, Card, Dialog, Segmented, ToneMark, UsageTrack, buttonClass } from './components'
import { APPROVALS_ADDRESS, APPROVAL_KIND, approvalAddress } from './Approvals'
import { ScheduleDetail, detailTabOf } from './ScheduleDetail'
import { NewSchedule } from './ScheduleEdit'
import { useRoleGate } from './Profile'
import { PageHead, Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'
import './styles/automate-landing.css'

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
  onGo,
}: {
  to: string
  go: (to: string) => void
  children: ReactNode
  className?: string
  /** The link to the item this page has open: `aria-current`, so the choice is not colour alone. */
  current?: boolean
  /** Called before a plain click goes. */
  onGo?: () => void
}) {
  return (
    <a
      href={addressToPath(to)}
      className={className}
      aria-current={current ? 'true' : undefined}
      onClick={(e: MouseEvent) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        onGo?.()
        go(to)
      }}
    >
      {children}
    </a>
  )
}

/**
 * THE ROW A CLOSED SHEET HANDS FOCUS BACK TO (QA1 automate-03). Opening a
 * sheet draws the list again under it, and closing it draws the list again
 * without it, so the link that opened the sheet is gone by the time it
 * closes: the sheet leaves its schedule's id here as it closes (Close,
 * Escape, `‹ Schedules`), and the list focuses that row's link once it has
 * drawn its rows. Taken once, so a later visit to the list is not moved.
 */
let closedSheet: string | null = null

export function sheetClosed(scheduleId: string): void {
  closedSheet = scheduleId
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

const MONTH = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const
const two = (n: number) => String(n).padStart(2, '0')

/**
 * ONE FORMAT FOR A NEXT FIRING, ON THE FORM AS ON THE SHEET (QA2 automate2-07):
 * "Fri, Oct 30 09:11", the wall time the schedule's zone gives, read from the
 * preview's own offset ISO (no zone arithmetic in the browser), with Z when
 * the zone is UTC. The form drew "2026-10-30 09:11" beside "Fri, Oct 30, 09:11
 * AM your time" and the sheet "Oct 30 09:11".
 */
export function firingWallTime(iso: string, timezone: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (m === null) return iso
  const day = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))).getUTCDay()
  return `${WEEKDAY[day]}, ${MONTH[Number(m[2]) - 1] ?? m[2]} ${Number(m[3])} ${m[4]}:${m[5]}${timezone === 'UTC' ? 'Z' : ''}`
}

/**
 * The same firing in the reader's own zone, in the same format, or null when
 * it is the same wall time the schedule's zone gives: "your time" then only
 * repeats the line it sits beside (QA2 automate2-07).
 */
export function firingReaderTime(iso: string): string | null {
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return null
  const yours = `${WEEKDAY[d.getDay()]}, ${MONTH[d.getMonth()]} ${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`
  return yours === firingWallTime(iso, '') ? null : yours
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

/**
 * What a `:run` answer's firing came to, as one sentence led by the
 * schedule's name; nothing is assumed past the firing as written. ONE COLON,
 * AND THE NAME SAID ONCE (QA2 automate2-09): the caller used to join
 * `name: ` to a sentence that began `Fired: the firing ...`, which read
 * "Nightly issue sweep: Fired: the firing created 2 work items."
 */
export function firedWords(name: string, f: ScheduleFiring): string {
  if (f.skip !== null) {
    if (f.skip.code === 'DRY_RUN') return `${name} recorded a dry run: nothing was created. What it would create is in History.`
    if (f.skip.code === 'NOTHING_STARTED') return `${name} fired: nothing started. Why is on its row in History.`
    return `${name} did not run: the firing was skipped, ${f.skip.code}.`
  }
  if (f.state === 'awaiting_approval') return `${name} fired: it waits for approval in Approvals, holding no capacity.`
  if (f.state === 'queued') return `${name} fired: it is queued behind the live one (the overlap policy).`
  if (f.work.length > 0) return `${name} fired: it created ${pluralise(f.work.length, 'work item')}.`
  return `${name} fired: it is ${f.state.replace(/_/g, ' ')}.`
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

/** `PauseIn.reason`'s `max_length` (routes/schedules.py): a longer one is a 422. */
const PAUSE_REASON_MAX = 500

/**
 * A PRESSED BUTTON KEEPS FOCUS WHILE ITS REQUEST IS IN FLIGHT (QA2
 * automate2-16): `Button`'s `busy` disables it, and a browser drops the focus
 * of a button that turns disabled to <body>, so for the busy window a keyboard
 * reader was nowhere. Busy here is `aria-disabled` instead, drawn as `Button`
 * draws busy (the spinner, the busy word, the disabled treatment): the button
 * stays focusable, a press while busy does nothing, and `Said` takes focus
 * when the answer arrives.
 */
export function HeldButton({ busy, onClick, children, ...rest }: Omit<ComponentProps<typeof Button>, 'busy'> & { busy: false | string }) {
  const on = busy !== false
  return (
    <Button {...rest} className={on ? 'is-busy' : undefined} icon={on ? <span className="c-spin" aria-hidden /> : undefined} aria-disabled={on || undefined} onClick={on ? undefined : onClick}>
      {on ? busy : children}
    </Button>
  )
}

/**
 * A SCHEDULE'S ACTIONS, at the head of its sheet: Run now, Dry run, Pause or
 * Resume, and Delete. No list row draws them (frame A1), and a phone draws
 * them as the desktop does, wrapped (frame A7b, the picked mock-up: the old
 * page's More fold, VQ-029, went with frame A2). Run now asks first only when
 * `runSpends`; Pause asks for an optional reason, which the list and the
 * sheet's Why paused say; Delete always asks for the schedule's name typed,
 * which the route checks again (`confirm`). Each answer is said, and the page
 * reads the schedule again rather than patching what it drew. A disabled
 * schedule is an admin's to enable: Run now says so and Resume is not offered
 * (`:resume` is 403 `admin_required` for it).
 */
export function ScheduleActions({
  s,
  onDone,
  onDeleted,
}: {
  s: Schedule
  onDone: (notice: ActNotice) => void
  onDeleted?: (notice: ActNotice) => void
}) {
  const [busy, setBusy] = useState<ActKind | null>(null)
  const [asking, setAsking] = useState<'run' | 'pause' | 'delete' | null>(null)
  const [typed, setTyped] = useState('')
  const [reason, setReason] = useState('')
  const [refused, setRefused] = useState<string | null>(null)
  // A VIEWER CHANGES NO SCHEDULE (docs/design/workspace-roles.md §4.8): every
  // write in routes/schedules.py needs `member`. The server still decides.
  const roleGate = useRoleGate('member', 'run and change schedules')
  const disabled = roleGate ?? (s.state === 'disabled' ? 'An admin disabled this schedule.' : undefined)

  const inFlight = useRef(false)
  const act = async (kind: ActKind) => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      await acting(kind)
    } finally {
      inFlight.current = false
    }
  }
  const acting = async (kind: ActKind) => {
    setBusy(kind)
    setRefused(null)
    if (kind === 'run' || kind === 'dry') {
      const r = await runSchedule(s.schedule_id, { dry_run: kind === 'dry' })
      setBusy(null)
      setAsking(null)
      if (r.status === 'ok') return onDone({ tone: 'ok', text: firedWords(s.name, r.data.firing) })
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
    // THE REASON IS SENT ONLY WHEN ONE IS WRITTEN: the server says "paused" for none.
    const why = reason.trim()
    const r = kind === 'pause' ? await pauseSchedule(s.schedule_id, why === '' ? {} : { reason: why }) : await resumeSchedule(s.schedule_id)
    setBusy(null)
    setAsking(null)
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
    <div className="c-acts sc-head-acts" data-testid="sc-acts-head">
      <HeldButton kind="primary" busy={busy === 'run' ? 'Running…' : false} disabledReason={disabled} onClick={runNow}>
        Run now
      </HeldButton>
      <HeldButton busy={busy === 'dry' ? 'Running dry…' : false} disabledReason={disabled} onClick={() => void act('dry')}>
        Dry run
      </HeldButton>
      {s.state === 'enabled' && (
        <Button disabled={roleGate !== null} onClick={() => { setReason(''); setAsking('pause') }}>
          Pause
        </Button>
      )}
      {resumable && (
        <HeldButton busy={busy === 'resume' ? 'Resuming…' : false} disabled={roleGate !== null} onClick={() => void act('resume')}>
          Resume
        </HeldButton>
      )}
      <Button kind="danger" disabled={roleGate !== null} onClick={() => { setTyped(''); setRefused(null); setAsking('delete') }}>
        Delete…
      </Button>
      {asking === 'run' && (
        <Dialog
          title={`Run ${s.name} now?`}
          onClose={() => setAsking(null)}
          actions={
            <>
              <Button kind="ghost" onClick={() => setAsking(null)}>
                Not now
              </Button>
              <HeldButton kind="primary" busy={busy === 'run' ? 'Running…' : false} onClick={() => void act('run')}>
                Run now
              </HeldButton>
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
      {asking === 'pause' && (
        <Dialog
          title={`Pause ${s.name}?`}
          onClose={() => setAsking(null)}
          actions={
            <>
              <Button kind="ghost" onClick={() => setAsking(null)}>
                Keep it running
              </Button>
              <HeldButton kind="primary" busy={busy === 'pause' ? 'Pausing…' : false} onClick={() => void act('pause')}>
                Pause
              </HeldButton>
            </>
          }
        >
          <p>No new firings until it is resumed. Work it already made carries on.</p>
          <label className="c-field">
            <span className="c-lbl">Why, in one line (optional; shown in the list and on this sheet)</span>
            <input className="c-inp" maxLength={PAUSE_REASON_MAX} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </Dialog>
      )}
      {asking === 'delete' && (
        <Dialog
          title={`Delete ${s.name}?`}
          onClose={() => setAsking(null)}
          actions={
            // ONE ROW ON A DESK, STACKED FULL WIDTH ON A PHONE (QA2 automate2-21):
            // the dialog's actions wrapped Keep it alone to the right and the
            // confirming button with its reason to a second row at the left,
            // both under the 44px target (automate-sheet.css).
            <div className="sc-del-acts">
              <Button kind="ghost" onClick={() => setAsking(null)}>
                Keep it
              </Button>
              <HeldButton
                kind="danger-filled"
                busy={busy === 'delete' ? 'Deleting…' : false}
                disabledReason={typed.trim() === s.name.trim() ? undefined : 'Type the schedule’s name exactly.'}
                onClick={() => void act('delete')}
              >
                Delete schedule
              </HeldButton>
            </div>
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

/**
 * What the last action said: a status, or an alert when it was refused.
 * IT TAKES FOCUS WHEN IT ARRIVES (QA1 automate-15): the pressed button holds
 * focus while busy (`HeldButton`), but may be gone once the schedule is read
 * again (Pause gives way to Resume, a dialog closes), and focus would drop to
 * <body>, so a keyboard reader started again from the top of the page.
 */
export function Said({ notice }: { notice: ActNotice | null }) {
  const line = useRef<HTMLParagraphElement | null>(null)
  useEffect(() => {
    if (notice !== null) line.current?.focus()
  }, [notice])
  if (notice === null) return null
  return (
    <p ref={line} tabIndex={-1} className={`sc-said is-${notice.tone}`} role={notice.tone === 'bad' ? 'alert' : 'status'} data-testid="sc-said">
      {notice.text}
    </p>
  )
}

// ---------------------------------------------------------------------------
// The landing (Automate proposal A, frame A1; AUTO-A1, part of #2417): the
// lead, the schedules with each one's state word, and the approvals card.
// ---------------------------------------------------------------------------

/**
 * `state_word` (`schedules.py::STATE_WORDS`, PR 2500): what a schedule needs,
 * derived once on the server, so the list, its filter's counts and the
 * summary cannot disagree. Nothing here derives it again.
 */
export type StateWord = 'active' | 'paused' | 'paused_by_tripwire' | 'waiting_on_approval' | 'failing' | 'never_fired'

/** A schedule as `GET /v1/schedules` lists it; an API older than PR 2500 serves no `state_word`. */
type Listed = Schedule & { state_word?: StateWord }

/**
 * `GET /v1/schedules`'s `summary` (`schedules.summarize`). `pending_approvals`
 * counts EVERY inbox item, not only the schedules' (BGD, 2026-10-26), and is
 * null when the inbox could not be read, `unread` saying why: an unknown
 * count is never 0. `oldest_expires_at` is the OLDEST item's expiry, null
 * when nothing waits or the oldest never expires.
 */
interface ListSummary {
  by_state: Partial<Record<StateWord, number>>
  pending_approvals: number | null
  oldest_expires_at: string | null
  unread?: string | null
}

/** The words as the reader reads them. A disabled schedule reads paused (BGD, 2026-10-26). */
export const STATE_WORD_SAID: Readonly<Record<StateWord, string>> = {
  active: 'active',
  paused: 'paused',
  paused_by_tripwire: 'paused by its tripwire',
  waiting_on_approval: 'waiting on approval',
  failing: 'failing',
  never_fired: 'never fired',
}

export const WORD_TONE: Readonly<Record<StateWord, string>> = {
  active: 'ok',
  paused: 'paused',
  paused_by_tripwire: 'warn',
  waiting_on_approval: 'info',
  failing: 'bad',
  never_fired: 'info',
}

/**
 * Why a listed schedule has no next firing, in its WORD (frame A1: "— paused
 * by its tripwire"), so the row never says disabled or auto-paused where its
 * word says paused. A word that is not a pause has a next firing unless the
 * tick has not set it yet. An API with no word falls back to NO_NEXT_RUN.
 */
function noNextFiring(s: Listed): string {
  switch (s.state_word) {
    case undefined:
      return NO_NEXT_RUN[s.state]
    case 'paused':
    case 'paused_by_tripwire':
      return `${STATE_WORD_SAID[s.state_word]}, so no next firing`
    default:
      return NO_NEXT_RUN.enabled
  }
}

/** The words that need a look: their rows sort first and are tinted (frame A1). */
const NEEDS_LOOK: ReadonlySet<StateWord> = new Set<StateWord>(['waiting_on_approval', 'paused_by_tripwire', 'failing'])

type Filter = 'all' | 'waiting_on_approval' | 'active' | 'failing' | 'paused' | 'never_fired'

/** The filter's segments, in the frame's order; Paused holds both paused words. */
const FILTERS: readonly { key: Exclude<Filter, 'all'>; label: string; words: readonly StateWord[] }[] = [
  { key: 'waiting_on_approval', label: 'Waiting on approval', words: ['waiting_on_approval'] },
  { key: 'active', label: 'Active', words: ['active'] },
  { key: 'failing', label: 'Failing', words: ['failing'] },
  { key: 'paused', label: 'Paused', words: ['paused', 'paused_by_tripwire'] },
  { key: 'never_fired', label: 'Never fired', words: ['never_fired'] },
]

/** An outcome's mark; a word the tick may add later is drawn as itself, unmarked by tone. */
const OUTCOME_TONE: Readonly<Record<string, string>> = { succeeded: 'ok', failed: 'bad', partial: 'warn', skipped: 'info' }

/** The inbox's waiting items that name this schedule (`subject.schedule_id`), oldest first as served. */
function itemsOf(inbox: ApprovalItem[] | null, id: string): ApprovalItem[] {
  return inbox === null ? [] : inbox.filter((i) => i.subject.schedule_id === id)
}

/** The line under a state word: the API's own pause copy or count, never a figure it did not serve. */
function wordLine(s: Listed, word: StateWord, mine: ApprovalItem[], now: number): string | null {
  switch (word) {
    case 'failing': {
      const n = s.consecutive_failures ?? 0
      const max = s.policy.max_consecutive_failures
      return `${n} failed in a row${max === undefined ? '' : `; pauses itself at ${max}`}`
    }
    case 'waiting_on_approval':
      return mine.length > 0 ? mine[0]!.summary : null
    case 'paused_by_tripwire': {
      const why = s.pause?.reason || 'a run cost more than its per-run tripwire'
      return s.pause?.at ? `${timeAgo(s.pause.at, now)}: ${why}` : why
    }
    case 'paused':
      if (s.state === 'disabled') {
        const why = s.pause?.reason || null
        return `An admin disabled it${why === null ? '' : `: ${why}`}; only an admin turns it back on`
      }
      return stateLine(s)
    case 'never_fired':
      return s.next_run_at === null ? null : `its first firing is ${untilWords(s.next_run_at, now)}`
    case 'active':
      return null
  }
}

function StateCell({ s, mine, now }: { s: Listed; mine: ApprovalItem[]; now: number }) {
  // AN API OLDER THAN PR 2500 SERVES NO WORD: a dash with its reason, never
  // a word derived here. The pause note it does serve is still said.
  const line = s.state_word === undefined ? stateLine(s) : wordLine(s, s.state_word, mine, now)
  return (
    <>
      {s.state_word === undefined ? (
        <i className="ctl-em" title="This API serves no state_word.">&mdash; not served</i>
      ) : (
        <ToneMark tone={WORD_TONE[s.state_word]} className="al-word">
          {STATE_WORD_SAID[s.state_word]}
        </ToneMark>
      )}
      <DryRunChip s={s} />
      {line !== null && <span className="sc-sub sc-why">{line}</span>}
    </>
  )
}

/** Spend today against the day budget, the bar only when there is one. */
function SpendCell({ s }: { s: Schedule }) {
  const day = s.budget.per_day_usd
  return (
    <>
      <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
      {day !== undefined && day > 0 && (
        <span className="al-track">
          <UsageTrack pct={(s.spend_today.reported_usd / day) * 100} tone={s.state === 'auto_paused' ? 'is-warn' : undefined} />
        </span>
      )}
      <span className="sc-sub">{day === undefined ? 'no day budget' : `of ${usd(day)} a day`}</span>
    </>
  )
}

/** What waits on this schedule: the count it serves, and which kinds from the inbox when that was read. */
function WaitingCell({ s, mine, inboxRead }: { s: Schedule; mine: ApprovalItem[]; inboxRead: boolean }) {
  if (s.pending_approvals === 0) return <span className="al-dim">none</span>
  const kinds = [...new Set(mine.map((i) => (APPROVAL_KIND[i.kind] ?? i.kind).toLowerCase()))]
  return (
    <>
      <b className="al-fig">{s.pending_approvals}</b>
      <span className="sc-sub">{inboxRead ? kinds.join(', ') : 'which, not read'}</span>
    </>
  )
}

function ScheduleList({
  rows,
  names,
  inbox,
  go,
  open,
}: {
  rows: Listed[]
  names: ReadonlyMap<string, string> | null
  /** The inbox's items, or null when it was not read. */
  inbox: ApprovalItem[] | null
  go: (to: string) => void
  /** The schedule open in the sheet beside the list (frame A2), or null. */
  open: string | null
}) {
  const now = useNow()
  const table = useRef<HTMLTableElement | null>(null)
  useEffect(() => {
    if (open !== null || closedSheet === null) return
    const id = closedSheet
    closedSheet = null
    const tr = [...(table.current?.querySelectorAll<HTMLElement>('tr[data-schedule]') ?? [])].find((r) => r.dataset.schedule === id)
    tr?.querySelector<HTMLElement>('a.al-name')?.focus()
  }, [open])
  // NEEDS A LOOK FIRST, THEN BY NAME (frame A1). The API lists by name, and a
  // stable sort keeps that order inside each half.
  const sorted = [...rows].sort((a, b) => Number(NEEDS_LOOK.has(b.state_word!)) - Number(NEEDS_LOOK.has(a.state_word!)))
  return (
    <div className="sc-table-wrap al-wrap">
      <table className="sc-table al-table" ref={table}>
        <thead>
          <tr>
            <th scope="col" data-col="name">Schedule</th>
            <th scope="col" data-col="target">Target</th>
            <th scope="col" data-col="when">When</th>
            <th scope="col" data-col="next">Next firing</th>
            <th scope="col" data-col="last">Last firing</th>
            <th scope="col" data-col="spend">Spend today</th>
            <th scope="col" data-col="waiting">Waiting</th>
            <th scope="col" data-col="state">State</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => {
            const mine = itemsOf(inbox, s.schedule_id)
            const need = s.state_word !== undefined && NEEDS_LOOK.has(s.state_word)
            const lastAt = s.last_firing === null ? null : s.last_firing.ended_at ?? s.last_firing.slot
            return (
              <tr
                key={s.schedule_id}
                className={`sc-row al-row${s.schedule_id === open ? ' is-open' : need ? ' is-need' : ''}`}
                data-schedule={s.schedule_id}
                data-word={s.state_word}
              >
                <td data-label="Schedule" className="al-c-name">
                  <InApp go={go} to={scheduleAddress(s.schedule_id)} className="sc-name al-name" current={s.schedule_id === open}>{s.name}</InApp>
                  <span className="sc-sub">
                    {s.tier === null ? s.type : `${s.type} · ${s.tier}`}
                    <span className="al-ph"> · {s.words ?? s.cron}</span>
                  </span>
                </td>
                <td data-label="Target" className="al-c-more">{slashBreaks(scopeWords(s, names))}</td>
                <td data-label="When" className="al-c-more">
                  <span title={`${s.cron} · ${s.timezone}`}>{s.words ?? s.cron}</span>
                  <span className="sc-sub al-mono">{s.cron}</span>
                  <span className="sc-sub sc-zone">{slashBreaks(s.timezone)}</span>
                </td>
                <td data-label="Next firing" className="al-c-next">
                  {s.next_run_at === null ? (
                    <i className="ctl-em">&mdash; {noNextFiring(s)}</i>
                  ) : (
                    <span title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</span>
                  )}
                </td>
                <td data-label="Last firing" className="al-c-more">
                  {s.last_firing === null ? (
                    <i className="ctl-em">&mdash; never fired</i>
                  ) : (
                    <>
                      {s.last_firing.outcome === null ? (
                        <ToneMark tone="live">in progress</ToneMark>
                      ) : OUTCOME_TONE[s.last_firing.outcome] === undefined ? (
                        <span>{s.last_firing.outcome}</span>
                      ) : (
                        <ToneMark tone={OUTCOME_TONE[s.last_firing.outcome]!}>{s.last_firing.outcome}</ToneMark>
                      )}
                      {lastAt !== null && <span className="sc-sub" title={localTime(lastAt)}>{timeAgo(lastAt, now)}</span>}
                    </>
                  )}
                </td>
                <td data-label="Spend today" className="al-c-spend"><SpendCell s={s} /></td>
                <td data-label="Waiting" className="al-c-more"><WaitingCell s={s} mine={mine} inboxRead={inbox !== null} /></td>
                <td data-label="State" className="al-c-state"><StateCell s={s} mine={mine} now={now} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** The filter's counts: the summary's when served, else the listed words counted; null when neither says. */
function wordCounts(rows: Listed[], summary: ListSummary | null): Partial<Record<StateWord, number>> | null {
  if (summary !== null) return summary.by_state
  if (rows.some((s) => s.state_word === undefined)) return null
  const counts: Partial<Record<StateWord, number>> = {}
  for (const s of rows) counts[s.state_word!] = (counts[s.state_word!] ?? 0) + 1
  return counts
}

/** "All · 7 | Waiting on approval · 2 | …": a segment for each word some schedule wears. */
function WordFilter({ rows, counts, value, onChange }: { rows: Listed[]; counts: Partial<Record<StateWord, number>>; value: Filter; onChange: (f: Filter) => void }) {
  const options = [
    { key: 'all' as Filter, label: 'All', count: rows.length },
    ...FILTERS.map((f) => ({ key: f.key as Filter, label: f.label, count: f.words.reduce((n, w) => n + (counts[w] ?? 0), 0) })).filter((o) => o.count > 0),
  ]
  return (
    <div className="al-filter">
      <Segmented<Filter> label="Schedules by state" small value={value} options={options} onChange={onChange} />
      <span className="al-order">Needs a look first, then by name</span>
    </div>
  )
}

/** The inbox's oldest item as this page read it, when it read one. */
interface InboxRead {
  items: ApprovalItem[] | null
  /** Why the inbox was not read, when it was not. */
  why: string | null
}

function readInbox(r: Result<{ approvals: ApprovalItem[] }>): InboxRead {
  if (r.status === 'ok' || r.status === 'stale') return { items: r.data.approvals, why: null }
  if (r.status === 'empty') return { items: [], why: null }
  if (r.status === 'error') return { items: null, why: `GET /v1/approvals failed: ${errorHeading(r.error)}` }
  return { items: null, why: 'the approvals read had not finished' }
}

/** Every inbox item waiting: the summary's count, else the inbox read's length, else null (unknown, never 0). */
function pendingCount(summary: ListSummary | null, inbox: InboxRead): number | null {
  if (summary !== null) return summary.pending_approvals
  return inbox.items === null ? null : inbox.items.length
}

/** The oldest item's expiry: the summary's, else the inbox's first item's. */
function oldestExpiry(summary: ListSummary | null, inbox: InboxRead): string | null {
  if (summary !== null) return summary.oldest_expires_at
  return inbox.items?.[0]?.expires_at ?? null
}

function Expiry({ at, now }: { at: string | null; now: number }) {
  if (at === null) return <span>does not expire</span>
  return <span title={localTime(at)}>{untilWords(at, now) === 'due now' ? 'expiring now' : untilWords(at, now).replace(/^in /, '')}</span>
}

/** "A, B and C". */
function listWords(parts: string[]): string {
  return parts.length <= 1 ? (parts[0] ?? '') : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
}

/**
 * THE LEAD (frame A1): what needs the reader across both tabs, in one
 * sentence, and the one button that starts on it. The approvals count is
 * every inbox item; the schedules named are the ones paused by their
 * tripwire or failing. When the inbox could not be read, the lead says so
 * rather than counting nothing (frame A6c).
 */
function LandingLead({ rows, summary, inbox, go }: { rows: Listed[]; summary: ListSummary | null; inbox: InboxRead; go: (to: string) => void }) {
  const now = useNow()
  const pending = pendingCount(summary, inbox)
  const tripped = rows.filter((s) => s.state_word === 'paused_by_tripwire')
  const failing = rows.filter((s) => s.state_word === 'failing')
  if (pending === null) {
    const why = summary?.unread ?? inbox.why ?? 'the approvals inbox could not be read'
    return (
      <Lead tone="bad" kicker="Could not read" title="Approvals could not be read, so the items waiting on you cannot be opened.">
        {`${why[0]!.toUpperCase()}${why.slice(1)}. The schedules were read; how many items wait, and when the oldest expires, is not known until the read works.`}
      </Lead>
    )
  }
  const parts: string[] = []
  if (pending > 0) parts.push(`${pending === 1 ? '1 approval waits' : `${pending} approvals wait`} on you`)
  if (tripped.length === 1) parts.push(`${tripped[0]!.name} is paused by its tripwire`)
  if (tripped.length > 1) parts.push(`${tripped.length} schedules are paused by their tripwires`)
  if (failing.length === 1) parts.push(`${failing[0]!.name} is failing`)
  if (failing.length > 1) parts.push(`${failing.length} schedules are failing`)
  if (parts.length === 0) {
    return (
      <Lead tone="ok" kicker="Nothing needs you" title="No approval waits, and no schedule is failing or paused by its tripwire.">
        Waiting items hold no capacity, so an empty inbox does not mean anything is stuck.
      </Lead>
    )
  }
  const oldest = inbox.items?.[0] ?? null
  const expires = oldestExpiry(summary, inbox)
  const said: ReactNode[] = []
  if (pending > 0) {
    const what = oldest === null ? 'The oldest' : `The oldest, ${oldest.summary},`
    said.push(
      expires === null ? (
        <Fragment key="exp">{what} does not expire. </Fragment>
      ) : (
        <Fragment key="exp">
          {what} expires <span title={localTime(expires)} data-testid="al-oldest-expiry">{untilWords(expires, now) === 'due now' ? 'now' : untilWords(expires, now)}</span>.{' '}
        </Fragment>
      ),
    )
  }
  if (tripped.length === 1) {
    const s = tripped[0]!
    said.push(
      <Fragment key="trip">
        {s.name} stopped{s.pause?.at ? ` ${timeAgo(s.pause.at, now)}` : ''}{s.pause?.reason ? `: ${s.pause.reason}` : ''}. Resume it from its page once you have read why.
      </Fragment>,
    )
  }
  const action =
    pending > 0 ? (
      <InApp go={go} to={oldest === null ? APPROVALS_ADDRESS : approvalAddress(oldest.approval_id)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
        {oldest === null ? 'Open Approvals' : 'Review the oldest approval'}
      </InApp>
    ) : (
      <InApp go={go} to={scheduleAddress((tripped[0] ?? failing[0])!.schedule_id)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
        Open {(tripped[0] ?? failing[0])!.name}
      </InApp>
    )
  const title = listWords(parts)
  return (
    <Lead tone="warn" kicker="Needs you" title={`${title[0]!.toUpperCase()}${title.slice(1)}.`} action={action}>
      {said}
    </Lead>
  )
}

/** The lead's frame: a kicker, one sentence, what explains it, and its one action. */
function Lead({ tone, kicker, title, action, children }: { tone: 'warn' | 'bad' | 'ok' | 'info'; kicker: string; title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className={`al-lead is-${tone}`} aria-label={kicker} data-testid="al-lead">
      <div className="al-lead-t">
        <span className="al-lead-k">{kicker}</span>
        <h2>{title}</h2>
        <p>{children}</p>
      </div>
      {action !== undefined && <div className="al-lead-a">{action}</div>}
    </section>
  )
}

/** How many inbox rows the card draws; the rest are one click away on Approvals. */
const CARD_ITEMS = 5

/**
 * THE APPROVALS CARD (frame A1): how many items wait -- every inbox item --
 * when the oldest expires, and the oldest few, each opening its decision
 * page. It draws no Approve or Reject: those are on the item's own page,
 * where `may_decide` says who may. A count that was not read is a dash with
 * its reason, never 0 (frame A6c).
 */
function ApprovalsCard({ summary, inbox, rows, go }: { summary: ListSummary | null; inbox: InboxRead; rows: Listed[]; go: (to: string) => void }) {
  const now = useNow()
  const pending = pendingCount(summary, inbox)
  const open = (
    <InApp go={go} to={APPROVALS_ADDRESS} className={buttonClass({ kind: 'secondary', size: 'sm' })}>
      Open Approvals
    </InApp>
  )
  if (pending === null) {
    const why = summary?.unread ?? inbox.why ?? 'the approvals inbox could not be read'
    return (
      <Card className="al-card al-approvals" title={<>Approvals · <span className="al-dim">&mdash; not read</span></>} action={open}>
        <p className="al-empty" data-testid="al-pending">
          &mdash; <span className="al-dim">not read: {why}</span>
        </p>
        <p className="al-note">Not 0: nothing was read. When the oldest expires is unknown too.</p>
      </Card>
    )
  }
  const names = new Map(rows.map((s) => [s.schedule_id, s.name]))
  const expires = oldestExpiry(summary, inbox)
  const shown = (inbox.items ?? []).slice(0, CARD_ITEMS)
  return (
    <Card className="al-card al-approvals" title={<span data-testid="al-pending">{`Approvals · ${pending} waiting`}</span>} action={open}>
      {pending === 0 ? (
        <p className="al-note">Nothing waits on you: the inbox was read and is empty. Waiting items hold no capacity.</p>
      ) : (
        <>
          <p className="al-note" data-testid="al-card-expiry">
            {expires === null ? (
              'The oldest does not expire.'
            ) : (
              <>
                The oldest expires <span title={localTime(expires)}>{untilWords(expires, now) === 'due now' ? 'now' : untilWords(expires, now)}</span>.
              </>
            )}{' '}
            Waiting items hold no capacity.
          </p>
          {inbox.items === null ? (
            <p className="al-note">Which items they are was not read: {inbox.why}.</p>
          ) : (
            shown.length > 0 && (
              <div className="sc-table-wrap">
                <table className="sc-table al-table is-inbox">
                  <thead>
                    <tr>
                      <th scope="col" data-col="what">What</th>
                      <th scope="col" data-col="from">From</th>
                      <th scope="col" data-col="expires">Expires in</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((item, i) => {
                      const sid = item.subject.schedule_id
                      return (
                        <tr key={item.approval_id} className={`sc-row al-row${i === 0 ? ' is-need' : ''}`} data-approval={item.approval_id}>
                          <td data-label="What">
                            <InApp go={go} to={approvalAddress(item.approval_id)} className="sc-name al-name">{item.summary}</InApp>
                            <span className="sc-sub">{APPROVAL_KIND[item.kind] ?? item.kind}</span>
                          </td>
                          <td data-label="From">
                            {typeof sid === 'string' && sid !== '' ? names.get(sid) ?? sid : <span className="al-dim">not from a schedule</span>}
                          </td>
                          <td data-label="Expires in" className="al-fig"><Expiry at={item.expires_at} now={now} /></td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )
          )}
          {inbox.items !== null && inbox.items.length > shown.length && (
            <p className="al-note">The oldest {shown.length} of {inbox.items.length}; the rest are on Approvals.</p>
          )}
        </>
      )}
    </Card>
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

/** What the landing read: the list and its summary, the inbox, and the built-in rows. */
interface Landing {
  schedules: Listed[]
  tenant_id: string
  /** null from an API older than PR 2500, which serves none. */
  summary: ListSummary | null
  repos: Result<RepoRecord[]>
  inbox: Result<{ approvals: ApprovalItem[] }>
  /** `GET /v1/schedules` answered 404: this deployment does not serve Automate (frame A6b). */
  notServed: boolean
}

/**
 * A SCHEDULE'S NAME BY ITS ID, as the list last read it (QA2 automate2-15):
 * the tab's title and an approval's "What asked for it" named a schedule by
 * its id ("sch_a · Schedules") though the list under the open sheet had read
 * its name. The list records every name it reads; `useScheduleName` gives the
 * name for an id, or null until one is read, and the caller keeps the id then.
 */
const SCHEDULE_NAMES = new Map<string, string>()
export const SCHEDULE_NAMES_EVENT = 'swarm:schedule-names'

export function rememberScheduleNames(rows: readonly Pick<Schedule, 'schedule_id' | 'name'>[]): void {
  let changed = false
  for (const r of rows) {
    if (r.name.trim() === '' || SCHEDULE_NAMES.get(r.schedule_id) === r.name) continue
    SCHEDULE_NAMES.set(r.schedule_id, r.name)
    changed = true
  }
  if (changed) globalThis.dispatchEvent?.(new Event(SCHEDULE_NAMES_EVENT))
}

export function scheduleNameOf(id: string): string | null {
  return SCHEDULE_NAMES.get(id) ?? null
}

export function useScheduleName(id: string | null): string | null {
  const [name, setName] = useState<string | null>(() => (id === null ? null : scheduleNameOf(id)))
  useEffect(() => {
    const again = () => setName(id === null ? null : scheduleNameOf(id))
    again()
    globalThis.addEventListener?.(SCHEDULE_NAMES_EVENT, again)
    return () => globalThis.removeEventListener?.(SCHEDULE_NAMES_EVENT, again)
  }, [id])
  return name
}

function SchedulesBody({ d, go, scopeSwitch, open }: { d: Landing; go: (to: string) => void; scopeSwitch: ReactNode; open: string | null }) {
  const [filter, setFilter] = useState<Filter>('all')
  const rows = d.schedules
  useEffect(() => rememberScheduleNames(rows), [rows])
  const repos = d.repos
  const names = repos.status === 'ok' || repos.status === 'stale' ? new Map(repos.data.map((r) => [r.repo_id, `${r.owner}/${r.repo}`])) : null
  const roleGate = useRoleGate('member', 'create schedules')
  const inbox = readInbox(d.inbox)
  const newSchedule =
    roleGate === null ? (
      <InApp go={go} to={scheduleAddress(NEW_SCHEDULE)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
        New schedule
      </InApp>
    ) : (
      <Button kind="primary" size="sm" disabledReason={roleGate}>
        New schedule
      </Button>
    )
  if (d.notServed) {
    return (
      <div className="sc-list al-page">
        <Lead tone="info" kicker="Not served here" title="This deployment does not serve Automate.">
          GET /v1/schedules answered 404, not found, so there are no schedules or approvals to show, and nothing here is counted. Schedules need an
          API with the schedules routes.
        </Lead>
        <Card className="al-card" title="Schedules">
          <p className="al-empty" data-testid="al-not-served">
            &mdash; <span className="al-dim">not served by this deployment</span>
          </p>
          <p className="al-note">The count is a dash, not 0: no list was read.</p>
        </Card>
      </div>
    )
  }
  const counts = wordCounts(rows, d.summary)
  const words = FILTERS.find((f) => f.key === filter)?.words ?? null
  const shown = words === null ? rows : rows.filter((s) => s.state_word !== undefined && words.includes(s.state_word))
  return (
    <div className="sc-list al-page">
      {/* With no schedule, New schedule is the lead's one button (frame A6a), not drawn twice. */}
      <div className="c-acts sc-acts">
        {scopeSwitch}
        {rows.length > 0 && newSchedule}
      </div>
      {rows.length === 0 ? (
        <>
          <Lead tone="info" kicker="Nothing scheduled yet" title="This workspace has no schedules. Make the first one." action={newSchedule}>
            A schedule fires work on a cron: an issue sweep, a docs check, a visual QA run. Nothing runs until you make one.
          </Lead>
          <Card className="al-card" title="Schedules · 0">
            <Absent kind="zero" heading="No schedules yet" say="This workspace has no schedules: the list was read and is empty.">
              Each firing makes ordinary work: an issue run, an agent or a workflow, listed in Work like any other.
            </Absent>
          </Card>
        </>
      ) : (
        <>
          <LandingLead rows={rows} summary={d.summary} inbox={inbox} go={go} />
          {counts !== null && <WordFilter rows={rows} counts={counts} value={filter} onChange={setFilter} />}
          <ScheduleList rows={shown} names={names} inbox={inbox.items} go={go} open={open} />
        </>
      )}
      <div className="al-g2">
        <ApprovalsCard summary={d.summary} inbox={inbox} rows={rows} go={go} />
        <Card className="al-card al-builtin" title="Built-in recurring work">
          <p className="al-note">Repository index refreshes run on each repository’s own schedule and are set in its Settings, not here.</p>
          <BuiltInRows repos={repos} go={go} />
        </Card>
      </div>
    </div>
  )
}

/**
 * The list, its summary, the inbox and the repositories in one read, so the
 * Screen's ⟳ refreshes all of them (AUT-19). The list's own answer is the
 * screen's, except a 404, which is a deployment without the schedules routes
 * and is drawn as that (frame A6b) rather than as a failure. A failed inbox
 * or repositories read is its own card's to say, never the page's.
 */
async function loadListAndRepos(): Promise<Result<Landing>> {
  const [list, repos, inbox] = await Promise.all([loadSchedules(), loadRepositories(), loadApprovals()])
  if (list.status === 'error' && list.error.httpStatus === 404) {
    return { status: 'ok', fetchedAt: Date.now(), data: { schedules: [], tenant_id: '', summary: null, repos, inbox, notServed: true } }
  }
  if (list.status !== 'ok' && list.status !== 'stale') return list
  const served = list.data as typeof list.data & { schedules: Listed[]; summary?: ListSummary | null }
  return { ...list, data: { schedules: served.schedules, tenant_id: served.tenant_id, summary: served.summary ?? null, repos, inbox, notServed: false } }
}

export function SchedulesScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const scheduleId = scheduleOf(view)
  if (scheduleId === NEW_SCHEDULE) return <NewSchedule go={go} />
  if (scheduleId !== null) {
    const q = new URLSearchParams(view ?? '')
    if (q.get('edit') === '1') return <ScheduleDetail scheduleId={scheduleId} go={go} tab={detailTabOf(q.get('tab'))} editing />
    // ONE SCHEDULE OPENS IN A SIDE SHEET OVER THE LIST (frame A2): the list
    // stays drawn and in view on the left at desktop width; on a phone the
    // sheet is the page (automate-sheet.css).
    return (
      <div className="as-over" data-sheet={scheduleId}>
        <div className="as-under">
          <ScheduleListScreen go={go} scopeSwitch={null} open={scheduleId} />
        </div>
        <ScheduleDetail scheduleId={scheduleId} go={go} tab={detailTabOf(q.get('tab'))} />
      </div>
    )
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

function ScheduleListScreen({ go, scopeSwitch, open = null }: { go: (to: string) => void; scopeSwitch: ReactNode; open?: string | null }) {
  return (
    <Screen
      title="Schedules"
      load={loadListAndRepos}
      summary={(d) => (d.notServed ? 'schedules: — not served by this deployment' : `${pluralise(d.schedules.length, 'schedule')} · workspace ${d.tenant_id}`)}
    >
      {(d) => <SchedulesBody d={d} go={go} scopeSwitch={scopeSwitch} open={open} />}
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
