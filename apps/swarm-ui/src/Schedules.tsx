/**
 * AUTOMATE › SCHEDULES (docs/schedules.md §6, lane S7): the tenant's
 * schedules, read-only, beside the recurring work the platform runs for it on
 * its own (the index's cadence, §8.3), and Overview's "Waiting on you" card.
 *
 * THE LANDING IS PROPOSAL A's FRAME A1 (the owner's picks, 2026-10-26, #2417): a
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
  loadScheduleTypes,
  pauseSchedule,
  resumeSchedule,
  runSchedule,
  type ApprovalItem,
  type Schedule,
  type ScheduleFiring,
  type ScheduleTypeRow,
} from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { usd } from './measure'
import { addressToPath, scheduleTypeOf } from './paths'
import { Mark } from './primitives'
import type { RepoRecord } from './RepositoriesData'
import { Button, Dialog, EmptyState, Segmented, Tag, buttonClass } from './components'
import { Info } from './AccountsVerdict'
import { APPROVALS_ADDRESS, APPROVAL_KIND, approvalAddress } from './Approvals'
import { ScheduleDetail, detailTabOf } from './ScheduleDetail'
import { NewSchedule } from './ScheduleEdit'
import { servedTier } from './ScheduleTemplates'
import { useRoleGate } from './Profile'
import { PageHead, Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'
import './styles/automate-landing.css'
import './styles/automate-cards.css'

/**
 * The list's address, and one schedule's (`/automate/schedules/<id>`,
 * paths.ts). A tab other than History rides as `tab=`
 * (`/automate/schedules/<id>?tab=gate`) and the editor as `edit=1`
 * (`/automate/schedules/<id>/edit`), so a reload or a link lands there (AUT-11).
 */
export const SCHEDULES_ADDRESS = 'automate/schedules'
export function scheduleAddress(id: string, at: { tab?: string; edit?: boolean; type?: string } = {}): string {
  const q = new URLSearchParams({ schedule: id })
  if (at.tab !== undefined && at.tab !== 'history') q.set('tab', at.tab)
  if (at.edit === true) q.set('edit', '1')
  if (at.type !== undefined) q.set('type', at.type)
  return `${SCHEDULES_ADDRESS}?${q.toString()}`
}

/**
 * The create form's address: `/automate/schedules/new`. A schedule id is `sch_`
 * and twelve hex digits (`schedules.new_schedule_id`), so `new` names none.
 * `?type=<name>` opens it with that type chosen (`scheduleAddress(NEW_SCHEDULE,
 * { type })`, a starter card's Use).
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
 * "Mine | All workspaces" (owner on #2157, 2026-10-23: Automate › Schedules keeps
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
 * counts EVERY inbox item, not only the schedules' (owner, 2026-10-26), and is
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

/** The words as the reader reads them. A disabled schedule reads paused (owner, 2026-10-26). */
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

type Filter = 'all' | 'needs' | 'active' | 'paused' | 'never_fired'

/** The filter's segments, in the cards mock-up's order; Needs you holds the three words that ask for something. */
const FILTERS: readonly { key: Exclude<Filter, 'all'>; label: string; words: readonly StateWord[] }[] = [
  { key: 'needs', label: 'Needs you', words: ['waiting_on_approval', 'paused_by_tripwire', 'failing'] },
  { key: 'active', label: 'Active', words: ['active'] },
  { key: 'paused', label: 'Paused', words: ['paused'] },
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

/**
 * THE WORD, ONLY WHERE THE SCHEDULE ASKS FOR SOMETHING (the cards pick,
 * 2026-11-12): waiting on approval, paused by its tripwire and failing carry
 * their word; active, paused and never fired say nothing, so a word on a card
 * always means "look". The tooltip carries what the word means.
 */
export const ASKS: Readonly<Partial<Record<StateWord, { word: string; tone: 'info' | 'warn' | 'bad'; tip: string }>>> = {
  waiting_on_approval: { word: 'Waiting on approval', tone: 'info', tip: 'A firing holds work until an approver decides. It expires if nobody does.' },
  paused_by_tripwire: { word: 'Paused by its tripwire', tone: 'warn', tip: 'Spend reached a tripwire, so the schedule paused itself. It stays paused until someone resumes it.' },
  failing: { word: 'Failing', tone: 'bad', tip: 'The last firings failed in a row. At the limit the schedule pauses itself.' },
}

/** The card's word and the API's own line for it as the word's tooltip; nothing for a word that asks for nothing. */
export function AskWord({ s, mine, now }: { s: Listed; mine: ApprovalItem[]; now: number }) {
  const ask = s.state_word === undefined ? undefined : ASKS[s.state_word]
  if (ask === undefined) return null
  const line = wordLine(s, s.state_word!, mine, now)
  return (
    <span className={`acd-word is-${ask.tone}`} data-testid="acd-word">
      <span className={`acd-dot is-${ask.tone}`} aria-hidden />
      {ask.word}
      {s.state_word === 'failing' && <span className="acd-dim"> &middot; {s.consecutive_failures ?? 0} in a row</span>}
      <Info about={ask.word} tip={line === null ? ask.tip : `${line}. ${ask.tip}`} />
    </span>
  )
}

/**
 * SPEND TODAY AGAINST THE TRIPWIRE: the bar's full width IS the per-day
 * budget, so a full bar is a schedule at its tripwire. A spend the API did
 * not serve is a dash with its reason, never $0.00; a schedule with no
 * per-day budget draws its figure and no bar, since there is no width to
 * measure it against.
 */
export function SpendBar({ spend, perDay, big = false }: { spend: Schedule['spend_today'] | undefined; perDay: number | undefined; big?: boolean }) {
  const cap = perDay !== undefined && perDay > 0 ? perDay : null
  if (spend === undefined || spend === null) {
    return (
      <div className={`acd-spend${big ? ' is-big' : ''}`} data-testid="acd-spend">
        <div className="acd-spend-t">
          <span className="acd-fig">&mdash;</span>
          <span className="acd-why">not served by this API</span>
        </div>
      </div>
    )
  }
  const used = spend.reported_usd
  const pct = cap === null ? 0 : Math.min(100, (used / cap) * 100)
  const tone = pct >= 100 ? ' is-bad' : pct >= 80 ? ' is-warn' : ''
  return (
    <div className={`acd-spend${big ? ' is-big' : ''}`} data-testid="acd-spend">
      <div className="acd-spend-t">
        <span className="acd-fig">{usd(used)}</span>
        <span className="acd-dim">{cap === null ? 'today · no tripwire' : `of ${usd(cap)} tripwire today`}</span>
        {spend.unreported_attempts > 0 && (
          <Info about="Unreported" tip={`${pluralise(spend.unreported_attempts, 'attempt')} did not report a cost, so the figure is a floor.`} />
        )}
      </div>
      {cap !== null && (
        <span
          className={`acd-bar${tone}`}
          role="meter"
          aria-valuenow={used}
          aria-valuemin={0}
          aria-valuemax={cap}
          aria-label="spend today against the tripwire"
        >
          <i style={{ width: `${pct}%` }} />
        </span>
      )}
    </div>
  )
}

function Outcome({ outcome }: { outcome: string | null }) {
  if (outcome === null) return <span className="acd-out"><span className="acd-dot is-live" aria-hidden />in progress</span>
  return (
    <span className="acd-out">
      <span className={`acd-dot is-${OUTCOME_TONE[outcome] ?? 'paused'}`} aria-hidden />
      {outcome}
    </span>
  )
}

function ScheduleCard({ s, names, mine, go, open, now }: { s: Listed; names: ReadonlyMap<string, string> | null; mine: ApprovalItem[]; go: (to: string) => void; open: string | null; now: number }) {
  const ask = s.state_word === undefined ? undefined : ASKS[s.state_word]
  const lastAt = s.last_firing === null ? null : s.last_firing.ended_at ?? s.last_firing.slot
  return (
    <section
      className={`acd-card${ask === undefined ? '' : ` is-${ask.tone}`}${s.schedule_id === open ? ' is-open' : ''}`}
      aria-label={s.name}
      data-schedule={s.schedule_id}
      data-word={s.state_word}
    >
      <div className="acd-card-h">
        <Tag title={s.tier ?? undefined}>{s.type}</Tag>
        <h3>{s.name}</h3>
        <DryRunChip s={s} />
      </div>
      <AskWord s={s} mine={mine} now={now} />
      <div className="acd-next" data-testid="acd-next">
        <span className="acd-k">Next firing</span>
        {s.next_run_at === null ? (
          <span className="acd-big is-none">&mdash; {noNextFiring(s)}</span>
        ) : (
          <span className="acd-next-v">
            <span className="acd-big">{untilWords(s.next_run_at, now)}</span>
            <span className="acd-dim">{firingWallTime(s.next_run_at, s.timezone)}</span>
          </span>
        )}
      </div>
      <SpendBar spend={s.spend_today} perDay={s.budget.per_day_usd} />
      <p className="acd-line">
        <span>{slashBreaks(scopeWords(s, names))}</span>
        <span className="acd-dim" title={`${s.cron} · ${s.timezone}`}>{s.words ?? s.cron}</span>
      </p>
      <div className="acd-card-f">
        {s.last_firing === null ? (
          <span className="acd-dim">never fired</span>
        ) : (
          <span className="acd-dim">
            <Outcome outcome={s.last_firing.outcome} /> {lastAt !== null && <span title={localTime(lastAt)}>{timeAgo(lastAt, now)}</span>}
          </span>
        )}
        <InApp go={go} to={scheduleAddress(s.schedule_id)} className={`acd-manage ${buttonClass({ kind: 'secondary', size: 'sm' })}`} current={s.schedule_id === open}>
          Manage
        </InApp>
      </div>
    </section>
  )
}

/** The card grid, the ones that ask for something first, then by name as the API lists them. */
function ScheduleCards({ rows, names, inbox, go, open }: { rows: Listed[]; names: ReadonlyMap<string, string> | null; inbox: ApprovalItem[] | null; go: (to: string) => void; open: string | null }) {
  const now = useNow()
  const grid = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (open !== null || closedSheet === null) return
    const id = closedSheet
    closedSheet = null
    const card = [...(grid.current?.querySelectorAll<HTMLElement>('[data-schedule]') ?? [])].find((r) => r.dataset.schedule === id)
    card?.querySelector<HTMLElement>('a.acd-manage')?.focus()
  }, [open])
  const sorted = [...rows].sort((a, b) => Number(NEEDS_LOOK.has(b.state_word!)) - Number(NEEDS_LOOK.has(a.state_word!)))
  return (
    <div className="acd-grid" ref={grid}>
      {sorted.map((s) => (
        <ScheduleCard key={s.schedule_id} s={s} names={names} mine={itemsOf(inbox, s.schedule_id)} go={go} open={open} now={now} />
      ))}
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

/** "All · 7 | Needs you · 3 | …": a segment for each filter some schedule falls in. */
function WordFilter({ rows, counts, value, onChange }: { rows: Listed[]; counts: Partial<Record<StateWord, number>>; value: Filter; onChange: (f: Filter) => void }) {
  const options = [
    { key: 'all' as Filter, label: 'All', count: rows.length },
    ...FILTERS.map((f) => ({ key: f.key as Filter, label: f.label, count: f.words.reduce((n, w) => n + (counts[w] ?? 0), 0) })).filter((o) => o.count > 0),
  ]
  return <Segmented<Filter> label="Show" small value={value} options={options} onChange={onChange} />
}

/** The inbox as this page read it: its items, oldest first, or why it was not read. */
interface InboxRead {
  items: ApprovalItem[] | null
  /** Why the inbox was not read, when it was not. */
  why: string | null
}

const askedAt = (i: ApprovalItem) => (i.requested_at === null ? Number.POSITIVE_INFINITY : new Date(i.requested_at).getTime())

function readInbox(r: Result<{ approvals: ApprovalItem[] }>): InboxRead {
  if (r.status === 'ok' || r.status === 'stale') return { items: [...r.data.approvals].sort((a, b) => askedAt(a) - askedAt(b)), why: null }
  if (r.status === 'empty') return { items: [], why: null }
  if (r.status === 'error') return { items: null, why: `GET /v1/approvals failed: ${errorHeading(r.error)}` }
  return { items: null, why: 'the approvals read had not finished' }
}

/** Every inbox item waiting: the summary's count, else the inbox read's length, else null (unknown, never 0). */
function pendingCount(summary: ListSummary | null, inbox: InboxRead): number | null {
  if (summary !== null) return summary.pending_approvals
  return inbox.items === null ? null : inbox.items.length
}

function unreadWhy(summary: ListSummary | null, inbox: InboxRead): string {
  return summary?.unread ?? inbox.why ?? 'the approvals inbox could not be read'
}

/** A figure that was not read: a dash, and its reason beside it and in its title. */
function Unread({ why }: { why: string }) {
  return (
    <span className="acd-unread" data-testid="acd-unread">
      <span className="acd-fig" title={why}>&mdash;</span> <span className="acd-why">{why}</span>
    </span>
  )
}

function Tile({ k, children, sub, tip, testid }: { k: string; children: ReactNode; sub?: ReactNode; tip?: string; testid: string }) {
  return (
    <div className="acd-tile" data-testid={testid}>
      <span className="acd-k">
        {k}
        {tip !== undefined && <Info about={k} tip={tip} />}
      </span>
      <span className="acd-tile-v">{children}</span>
      {sub !== undefined && sub !== null && <span className="acd-dim">{sub}</span>}
    </div>
  )
}

/**
 * THE ANSWER-FIRST ROW: how many schedules need the reader, the next firing
 * anywhere, and today's spend across every schedule. A count the API did not
 * serve (no `state_word`, no `spend_today`) is a dash with its reason.
 */
function AnswerRow({ rows, summary, inbox }: { rows: Listed[]; summary: ListSummary | null; inbox: InboxRead }) {
  const now = useNow()
  const pending = pendingCount(summary, inbox)
  const worded = !rows.some((s) => s.state_word === undefined)
  const needs = rows.filter((s) => s.state_word !== undefined && NEEDS_LOOK.has(s.state_word))
  const tripped = rows.filter((s) => s.state_word === 'paused_by_tripwire').length
  const failing = rows.filter((s) => s.state_word === 'failing').length
  const parts = [
    // An inbox that was not read is said, never left out as if none waited (visual QA).
    pending === null ? 'approvals not read' : pending > 0 ? pluralise(pending, 'approval') : null,
    tripped > 0 ? pluralise(tripped, 'tripwire') : null,
    failing > 0 ? `${failing} failing` : null,
  ].filter((p): p is string => p !== null)
  const next = rows.filter((s) => s.next_run_at !== null).sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())[0]
  const unspent = rows.some((s) => s.spend_today === undefined || s.spend_today === null)
  const spent = unspent ? 0 : rows.reduce((n, s) => n + s.spend_today.reported_usd, 0)
  const caps = rows.reduce((n, s) => n + (s.budget.per_day_usd ?? 0), 0)
  return (
    <div className="acd-answer">
      <Tile k="Needs you" testid="acd-needs" sub={worded && parts.length > 0 ? parts.join(' · ') : undefined}>
        {worded ? (
          <>
            <span className="acd-huge">{needs.length}</span>
            <span className="acd-dim"> of {pluralise(rows.length, 'schedule')}</span>
          </>
        ) : (
          <Unread why="this API serves no state_word" />
        )}
      </Tile>
      <Tile k="Next firing" testid="acd-next-any" sub={next === undefined ? undefined : `${next.name} · ${firingWallTime(next.next_run_at!, next.timezone)}`}>
        {next === undefined ? <span className="acd-dim">&mdash; none scheduled</span> : untilWords(next.next_run_at!, now)}
      </Tile>
      <Tile
        k="Spend today"
        testid="acd-spend-today"
        tip="Reported spend today, summed over every schedule. Each schedule pauses itself at its own per-day budget."
        sub={unspent || caps === 0 ? undefined : `of ${usd(caps)} across every tripwire`}
      >
        {unspent ? <Unread why="a schedule's spend is not served by this API" /> : usd(spent)}
      </Tile>
    </div>
  )
}

/**
 * WAITING ON YOU: every inbox item, oldest first, each with its expiry and
 * Review, which opens the item's own page -- where `may_decide` holds the
 * approvers rule and the expiry. Nothing is approved or denied from here. An
 * inbox that was not read is a dash with its reason, never "nothing waiting".
 */
function WaitingPanel({ summary, inbox, rows, go }: { summary: ListSummary | null; inbox: InboxRead; rows: Listed[]; go: (to: string) => void }) {
  const now = useNow()
  const pending = pendingCount(summary, inbox)
  const names = new Map(rows.map((s) => [s.schedule_id, s.name]))
  const all = (
    <InApp go={go} to={APPROVALS_ADDRESS} className={buttonClass({ kind: 'secondary', size: 'sm' })}>
      All approvals
    </InApp>
  )
  return (
    <section className="acd-panel is-tight" aria-label="Waiting on you" data-testid="acd-waiting">
      <div className="acd-panel-h">
        <h2>Waiting on you</h2>
        {pending !== null && pending > 0 && <span className="acd-dim" data-testid="acd-pending">{pending}, oldest first</span>}
        <span className="acd-grow" />
        {all}
      </div>
      {pending === null || inbox.items === null ? (
        <p className="acd-nothing">
          <Unread why={pending === null ? unreadWhy(summary, inbox) : inbox.why ?? 'the approvals inbox could not be read'} />
        </p>
      ) : inbox.items.length === 0 ? (
        <p className="acd-nothing" data-testid="acd-nothing-waiting">
          <span className="acd-dot is-ok" aria-hidden /> Nothing waiting
        </p>
      ) : (
        <ul className="acd-wrows">
          {inbox.items.map((item) => {
            const sid = item.subject.schedule_id
            return (
              <li key={item.approval_id} data-approval={item.approval_id}>
                <Tag>{APPROVAL_KIND[item.kind] ?? item.kind}</Tag>
                <span className="acd-wt">{item.summary}</span>
                <span className="acd-dim">{typeof sid === 'string' && sid !== '' ? names.get(sid) ?? sid : 'by hand'}</span>
                <span className="acd-exp">
                  <span className="acd-k">Expires</span> <span className="acd-strong"><Expiry at={item.expires_at} now={now} /></span>
                </span>
                <InApp go={go} to={approvalAddress(item.approval_id)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
                  Review
                </InApp>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function Expiry({ at, now }: { at: string | null; now: number }) {
  if (at === null) return <span>never</span>
  return <span title={localTime(at)}>{untilWords(at, now) === 'due now' ? 'now' : untilWords(at, now)}</span>
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
      <div className="acd-page" data-testid="acd-not-served">
        <EmptyState kind="failed" heading="Automate is not served here">
          <span className="acd-mono">GET /v1/schedules</span> answered 404{' '}
          <Info about="Not served" tip="This deployment's API has no schedule routes. Schedules and approvals appear once it serves them." />
        </EmptyState>
      </div>
    )
  }
  if (rows.length === 0) {
    return (
      <div className="acd-page" data-testid="acd-none">
        {scopeSwitch !== null && <div className="acd-bar-row">{scopeSwitch}</div>}
        <EmptyState kind="empty" heading="No schedules" action={newSchedule}>
          <span className="acd-dim">Start from a type</span>{' '}
          <Info about="Schedules" tip="A schedule fires work on a cron, from a type. Nothing runs until you make one." />
        </EmptyState>
        <StarterCards go={go} roleGate={roleGate} />
        <WaitingPanel summary={d.summary} inbox={inbox} rows={rows} go={go} />
      </div>
    )
  }
  const counts = wordCounts(rows, d.summary)
  const words = FILTERS.find((f) => f.key === filter)?.words ?? null
  const shown = words === null ? rows : rows.filter((s) => s.state_word !== undefined && words.includes(s.state_word))
  return (
    <div className="acd-page">
      <AnswerRow rows={rows} summary={d.summary} inbox={inbox} />
      <WaitingPanel summary={d.summary} inbox={inbox} rows={rows} go={go} />
      <div className="acd-bar-row">
        {counts !== null && <WordFilter rows={rows} counts={counts} value={filter} onChange={setFilter} />}
        {scopeSwitch}
        <span className="acd-grow" />
        {newSchedule}
      </div>
      <ScheduleCards rows={shown} names={names} inbox={inbox.items} go={go} open={open} />
    </div>
  )
}

/**
 * THE STARTER CARDS (the cards mock-up's empty-none frame, the owner's pick
 * under #2417): with no schedules, one card per type the catalogue serves,
 * each with Use, which opens New schedule with that type already chosen. The
 * catalogue is read only here, so a page with schedules makes no extra read.
 * A type this account may not use is a card whose Use says why, as its tile
 * in the form does; a failed read leaves New schedule, which reads it again.
 */
function StarterCards({ go, roleGate }: { go: (to: string) => void; roleGate: string | null }) {
  const [types, setTypes] = useState<Result<{ types: ScheduleTypeRow[] }> | null>(null)
  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (live) setTypes(r)
    })
    return () => {
      live = false
    }
  }, [])
  if (types === null || (types.status !== 'ok' && types.status !== 'stale') || types.data.types.length === 0) return null
  return (
    <div className="acd-grid" data-testid="acd-starters">
      {types.data.types.map((t) => {
        const tier = servedTier(t)
        const why = roleGate ?? (t.available ? null : (t.disabled_reason ?? 'This type is not available to this account.'))
        return (
          <section key={t.name} className="acd-card acd-starter" aria-label={t.name}>
            <div className="acd-card-h">
              {tier !== null && <Tag>{tier}</Tag>}
              <h3 className="acd-mono">{t.name}</h3>
            </div>
            <div className="acd-card-f">
              {why === null ? (
                <InApp go={go} to={scheduleAddress(NEW_SCHEDULE, { type: t.name })} className={buttonClass({ size: 'sm' })}>
                  Use
                </InApp>
              ) : (
                <Button size="sm" disabledReason={why}>
                  Use
                </Button>
              )}
            </div>
          </section>
        )
      })}
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
  if (scheduleId === NEW_SCHEDULE) return <NewSchedule go={go} type={scheduleTypeOf(new URLSearchParams(view ?? '').get('type'))} />
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
