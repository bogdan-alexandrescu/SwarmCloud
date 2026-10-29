/**
 * ONE SCHEDULE, IN THE SIDE SHEET OVER THE LIST (Automate proposal A, frame
 * A2, with A6's never-fired state and A7's phone for this sheet; lane
 * AUTO-A2, part of #2417). The list stays in view on the left at desktop
 * width; on a phone the sheet is the page. Top to bottom: the name with its
 * state word, the actions, what it does with its type and target, its
 * cadence and next five firings, its firings with each one's runs and pull
 * requests and its largest run against the per-run tripwire, spend today
 * against the day budget, the gate and who approves; Settings and Audit fold
 * at the foot and open in place.
 *
 * Every value is `GET /v1/schedules/{id}` as served, except what the type
 * does (the catalogue), the next five firings (`POST /v1/schedules:preview`,
 * the tick's parser) and the audit (its own route, read when its fold is
 * opened). Two things write: Edit, which opens the editor's page
 * (ScheduleEdit.tsx), and the SD3 merge switch (§4.2). After either, the
 * sheet reads the schedule again rather than patching what it drew.
 *
 * A schedule that has never fired shows the `Absent` state, not "0 runs"
 * (§6.2): no firing is a fact about the schedule, and a zero would read as a
 * count of something that ran.
 */
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  loadMe,
  loadPeople,
  loadSchedule,
  loadScheduleAudit,
  loadScheduleTypes,
  loadRepositories,
  previewSchedule,
  setMergeMode,
  type FiringOwnLive,
  type FiringWork,
  type NothingStartedDetail,
  type Schedule,
  type ScheduleAuditEntry,
  type ScheduleBudgetUse,
  type ScheduleFiring,
  type ScheduleTypeRow,
} from './api'
import { Button, CIcon, Dialog, ToneMark } from './components'
import { WarnGlyph } from './components/glyphs'
import type { ApiError, Result } from './fetch'
import { usd } from './measure'
import { pluralise } from './types'
import { Absent, Mark } from './primitives'
import { addressToPath } from './paths'
import { dayWords, EditSchedule, HardStops, plainValue } from './ScheduleEdit'
import { gapWords } from './ScheduleTemplates'
import { HistoryTimeline, workAddress } from './ScheduleHistoryTimeline'
import {
  DryRunChip,
  InApp,
  SCHEDULES_ADDRESS,
  STATE_WORD,
  STATE_WORD_SAID,
  WORD_TONE,
  Said,
  ScheduleActions,
  SpendFigure,
  localTime,
  scheduleAddress,
  sheetClosed,
  slashBreaks,
  untilWords,
  type ActNotice,
  type StateWord,
} from './Schedules'
import { APPROVALS_ADDRESS } from './Approvals'
import { FailedPanel, Screen, timeAgo } from './Shell'
import type { RepoRecord } from './RepositoriesData'
import { useNow } from './useNow'
import './styles/automate-sheet.css'

export type DetailTab = 'history' | 'budget' | 'gate' | 'settings' | 'audit'

/** The tabs, in the order drawn; the first is the page's default and carries no `tab=`. */
export const DETAIL_TABS: readonly DetailTab[] = ['history', 'budget', 'gate', 'settings', 'audit']

/** A `tab=` value as a tab, or History for anything else (AUT-11). */
export function detailTabOf(v: string | null): DetailTab {
  return DETAIL_TABS.includes(v as DetailTab) ? (v as DetailTab) : 'history'
}

/**
 * A SKIP IN WORDS (QA2 automate2-03): `skipped: OVERLAP` printed the API's
 * code, and a skip served without one read `skipped: undefined`. Each of
 * `schedulefire`'s skip codes (§1.2) in words; an unknown or absent code is
 * plain `skipped`. `NOTHING_STARTED` has its own words below.
 */
const SKIP_WORDS: Readonly<Record<string, string>> = {
  OVERLAP: 'the last firing was still running',
  BUDGET_EXHAUSTED: 'the day budget was spent',
  MISSED_SLOT: 'its time had passed',
  OWNER_NOT_MEMBER: 'its owner is no longer a member',
  OWNER_ROLE_NARROWED: 'its owner may no longer submit work',
  REPOSITORY_NOT_GRANTED: 'a repository in its scope is not granted',
  SCHEDULES_DISABLED: 'schedules are switched off',
  DRY_RUN: 'a dry run, so nothing was started',
  SCHEDULE_NOT_ENABLED: 'the schedule was paused or disabled',
  TYPE_UNAVAILABLE: 'its type has left the catalogue',
  RUN_OVER_BUDGET: 'a run cost more than the per-run tripwire',
  CONSECUTIVE_FAILURES: 'too many firings failed in a row',
}

/** What a firing came to: its outcome once ended, its state while it is not. */
function firingWord(f: ScheduleFiring): string {
  if (f.skip?.code === 'NOTHING_STARTED') return `nothing started: ${nothingStartedWords(nothingDetail(f))}`
  if (f.skip !== null) {
    const why = typeof f.skip.code === 'string' ? SKIP_WORDS[f.skip.code] : undefined
    return why === undefined ? 'skipped' : `skipped: ${why}`
  }
  return f.outcome ?? f.state.replace(/_/g, ' ')
}

/**
 * WHY A FIRING STARTED NOTHING (lane SCHED). Measured 2026-10-11 on
 * sch_6ecbe1307511 and sch_88b97e090beb: rows that started nothing showed only
 * a state while the firing held its reason (`skip.detail`, #1133). Each
 * `issue_sweep.nothing_reason` code in words; an unknown one is shown as served.
 */
const NOTHING_REASON_WORDS: Record<string, string> = {
  no_repository: 'no repository is in its scope',
  no_open_issue: 'its repositories have no open issue',
  no_eligible_issue: 'every open issue was passed over',
  repositories_not_read: 'its repositories could not be read',
  executor_created_nothing: 'its type created nothing and gave no reason',
}

/** A limit's parameter in words, for "its … limit is reached". */
const BOUND_WORDS: Record<string, string> = {
  max_live_runs: 'live-runs',
  max_pending_plans: 'pending-plans',
  max_new_per_firing: 'new-per-firing',
  max_concurrent: 'concurrent-work',
  live_scan: 'live-run scan',
}

/** A passed-over kind (`SweepReport.skip`'s prefix) as "N <words>". */
const PASSED_WORDS: Record<string, [string, string]> = {
  author: ['was opened by someone outside the repository', 'were opened by someone outside the repository'],
  label: ['carries a label never swept', 'carry a label never swept'],
  excluded: ['is excluded', 'are excluded'],
  not_included: ['carries none of the listed labels', 'carry none of the listed labels'],
  live_run: ['already has a live run', 'already have a live run'],
  open_pull_request: ['is claimed by an open pull request', 'are claimed by an open pull request'],
  in_flight_workflow: ['in flight in a workflow', 'in flight in a workflow'],
  not_ready_unchanged: ['was not ready and has not changed', 'were not ready and have not changed'],
  unchanged_since_run: ['has not changed since its last run', 'have not changed since their last run'],
  room: ['waits for room', 'wait for room'],
}

function nothingDetail(f: ScheduleFiring): NothingStartedDetail {
  const d = f.skip?.detail
  return d !== null && typeof d === 'object' ? (d as NothingStartedDetail) : {}
}

/** The firing's own-limit count: its selection's, or the skip's when the selection was not served. */
function ownLive(f: ScheduleFiring): FiringOwnLive | null {
  return f.selection?.own_live ?? nothingDetail(f).own_live ?? null
}

export function nothingStartedWords(d: NothingStartedDetail): string {
  if (d.reason === 'no_room') {
    const bound = d.bound ?? d.own_live?.bound
    return bound ? `its ${BOUND_WORDS[bound] ?? bound.replace(/_/g, ' ')} limit is reached` : 'it had no room'
  }
  if (d.reason === undefined) return 'no reason was recorded'
  return NOTHING_REASON_WORDS[d.reason] ?? d.reason.replace(/_/g, ' ')
}

/** "N of M running from this schedule": its own runs, never the tenant's. */
export function ownLiveWords(o: FiringOwnLive): string {
  const what = o.bound === 'max_pending_plans' ? 'plans waiting' : 'running'
  return `${o.running} of ${o.limit} ${what} from this schedule`
}

/** The lines under a firing that started nothing: what was passed over, what could not be read. */
function nothingStartedLines(d: NothingStartedDetail): string[] {
  const lines: string[] = []
  const passed = Object.entries(d.passed_over_by_reason ?? {}).filter(([kind]) => kind !== 'room' || d.reason !== 'no_room')
  if (passed.length > 0) {
    lines.push(
      `passed over: ${passed
        .map(([kind, n]) => {
          const words = PASSED_WORDS[kind]
          return `${n} ${words ? words[n === 1 ? 0 : 1] : kind.replace(/_/g, ' ')}`
        })
        .join(', ')}`,
    )
  }
  for (const failure of d.failures ?? []) {
    lines.push(`could not read ${failure.repository ?? 'a repository'}: ${(failure.error ?? 'unknown error').replace(/_/g, ' ')}`)
  }
  for (const failure of d.start_failures ?? []) {
    lines.push(`${failure.issue ?? 'an issue'} not started: ${(failure.error ?? 'unknown error').replace(/_/g, ' ')}`)
  }
  if (d.truncated) lines.push('more work than one firing reads: some was not looked at')
  return lines
}

/**
 * A planning refusal's code in plain words. A refused run is an answer about
 * its issue, not a failure of the schedule: the firing stays `succeeded`, and
 * this line says what the issue asked for that could not be done.
 */
const REFUSAL_WORDS: Record<string, string> = {
  WORKFLOWS_PATH: 'its plan changes .github/workflows/ files, which SwarmCloud cannot push; a person must make that change',
}

export function refusalWords(code: string): string {
  return REFUSAL_WORDS[code] ?? `refused at planning (${code})`
}

/**
 * What a firing came to, and why when it started nothing or refused a run:
 * the table's Outcome cell and the timeline's lane head, so the two views
 * never disagree about a firing.
 */
function FiringOutcome({ f }: { f: ScheduleFiring }) {
  // Read whole, never `.sc-cut` (QA2-FIX-21 review): cut, an OVERLAP skip
  // read 'skipped: the la…' at 1440, its reason only in a hover title.
  return (
    <>
      <span className="sc-why">{firingWord(f)}</span>
      {f.approval_id !== null && f.state === 'awaiting_approval' && <span className="sc-sub">waiting for approval, holding nothing</span>}
      {f.skip?.code === 'NOTHING_STARTED' &&
        nothingStartedLines(nothingDetail(f)).map((line) => (
          <span key={line} className="sc-sub" data-nothing-started={nothingDetail(f).reason ?? ''}>
            {line}
          </span>
        ))}
      {ownLive(f) !== null && (
        <span className="sc-sub" data-own-live>
          {ownLiveWords(ownLive(f)!)}
        </span>
      )}
      {(f.refused_runs ?? []).map((r) => (
        <span key={r.run_id} className="sc-sub" data-refused={r.code}>
          {r.issue} not started: {refusalWords(r.code)}
        </span>
      ))}
    </>
  )
}

/**
 * A FIRING AS PROPOSAL A's SHEET SERVES IT (AUTO-A2, part of #2417): every
 * item of `work[]` that opened a pull request carries `pull_request {number,
 * state}`, and `largest_run_usd` is its costliest run, null with
 * `largest_run_unread` saying why (PR 2500). An API older than that serves
 * neither, and the cells say so rather than drawing a figure.
 */
type ServedWork = FiringWork & { pull_request?: { number: number; state: string } }
type ServedFiring = Omit<ScheduleFiring, 'work'> & {
  work: ServedWork[]
  largest_run_usd?: number | null
  largest_run_unread?: string | null
}

/** A pull request's state in the words a person reads (`routes/schedules.py::_pr_state`). */
const PR_WORDS: Readonly<Record<string, string>> = {
  merged: 'merged',
  waiting_on_approval: 'waiting to merge',
  closed: 'closed unmerged',
  open: 'open',
}

const TRIGGER_WORDS: Readonly<Record<string, string>> = {
  cron: 'on its cadence',
  catch_up: 'catch-up',
  run_now: 'run now',
  queued: 'queued',
}

const WORK_NOUN: Readonly<Record<string, string>> = { issue_run: 'run', task: 'task', workflow: 'workflow', api_action: 'action' }

/** "3 runs", "1 run, 1 action": what a firing made, by kind, in the order first made. */
function workCount(work: readonly FiringWork[]): string {
  const n = new Map<string, number>()
  for (const w of work) n.set(WORK_NOUN[w.kind] ?? w.kind, (n.get(WORK_NOUN[w.kind] ?? w.kind) ?? 0) + 1)
  return [...n].map(([noun, k]) => pluralise(k, noun)).join(', ')
}

/**
 * How late a firing was, when it fired a whole minute or more after its slot,
 * in the template card's duration words (QA2 automate2-19): `300 min late`
 * is `5 hours late`, and `1 day 1 hour late` beside `at least 1 day apart`.
 */
function lateWords(f: ScheduleFiring): string | null {
  if (f.slot === null || f.fired_at === null) return null
  const late = Math.floor((Date.parse(f.fired_at) - Date.parse(f.slot)) / 60_000)
  if (!Number.isFinite(late) || late < 1) return null
  const parts = [Math.floor(late / 1440) * 1440, Math.floor((late % 1440) / 60) * 60, late % 60].filter((m) => m > 0)
  return `${parts.map(gapWords).join(' ')} late`
}

/**
 * THE LARGEST RUN AGAINST THE PER-RUN TRIPWIRE (frame A2): the bar is drawn
 * on a scale a third past the tripwire, so the tick sits at three quarters
 * and a run over it is visibly past the line. Unknown is a dash with the
 * server's reason, never $0.
 */
function LargestRun({ f, perRun }: { f: ServedFiring; perRun: number | undefined }) {
  if (f.largest_run_usd === undefined) {
    return <i className="ctl-em" title="This API serves no largest_run_usd.">&mdash; not served</i>
  }
  if (f.largest_run_usd === null) {
    return <i className="ctl-em" data-largest="unknown">&mdash; {f.largest_run_unread ?? 'not known'}</i>
  }
  const largest = f.largest_run_usd
  if (perRun === undefined || perRun <= 0) {
    return (
      <>
        <span>{usd(largest)}</span>
        <span className="sc-sub">no per-run tripwire</span>
      </>
    )
  }
  const whole = (perRun * 4) / 3
  const over = largest > perRun
  return (
    <span className={`as-trip${over ? ' is-over' : ''}`} data-largest={over ? 'over' : 'under'}>
      <span className="as-trip-bar" role="img" aria-label={`Largest run ${usd(largest)} of the ${usd(perRun)} per-run tripwire${over ? ', over it' : ''}`}>
        <i style={{ width: share(largest, whole) }} />
        <s style={{ left: '75%' }} />
      </span>
      <span className="sc-sub">
        {usd(largest)} of {usd(perRun)}
        {over ? ', over' : ''}
      </span>
    </span>
  )
}

/** A firing's work, each item linked, with the pull request it opened. */
function WorkCell({ f, go }: { f: ServedFiring; go: (to: string) => void }) {
  if (f.work.length === 0) return <i className="ctl-em">nothing started</i>
  return (
    <>
      <span>{workCount(f.work)}</span>
      <ul className="as-work">
        {f.work.map((w) => {
          const to = workAddress(w)
          const pr = w.pull_request
          return (
            <li key={`${w.kind}:${w.id}`} data-work={w.id}>
              {to === null ? <span className="mono">{w.id}</span> : <InApp go={go} to={to} className="mono">{w.id}</InApp>}
              {pr !== undefined ? (
                <span data-pr={pr.state}>
                  {' '}&rarr; PR #{pr.number} {PR_WORDS[pr.state] ?? pr.state}
                </span>
              ) : (
                w.outcome !== null && w.outcome !== undefined && <span className="al-dim"> {w.outcome}</span>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}

function FiringRows({ firings, perRun, go }: { firings: ServedFiring[]; perRun: number | undefined; go: (to: string) => void }) {
  const now = useNow()
  return (
    <div className="sc-table-wrap">
      <table className="sc-table is-firings as-firings">
        <thead>
          <tr>
            <th scope="col" data-col="slot">Firing</th>
            <th scope="col" data-col="outcome">Outcome</th>
            <th scope="col" data-col="work">Runs and pull requests</th>
            <th scope="col" data-col="cost">Cost</th>
            <th scope="col" data-col="largest">Largest run vs tripwire</th>
          </tr>
        </thead>
        <tbody>
          {firings.map((f) => {
            const at = f.fired_at ?? f.slot
            const late = lateWords(f as ScheduleFiring)
            return (
              <tr key={f.firing_id} className="sc-row" data-firing={f.firing_id}>
                <td data-label="Firing">
                  {at === null ? <i className="ctl-em">&mdash; not recorded</i> : <span title={localTime(at)}>{timeAgo(at, now)}</span>}
                  <span className="sc-sub">
                    {TRIGGER_WORDS[f.trigger] ?? f.trigger.replace(/_/g, ' ')}
                    {late === null ? '' : ` · ${late}`}
                  </span>
                </td>
                <td data-label="Outcome">
                  <FiringOutcome f={f as ScheduleFiring} />
                </td>
                <td data-label="Runs and pull requests">
                  <WorkCell f={f} go={go} />
                </td>
                <td data-label="Cost">
                  {f.cost === null ? (
                    <i className="ctl-em" title="No attempt of this firing has ended, or it created no work.">&mdash; none reported</i>
                  ) : (
                    <SpendFigure usdReported={f.cost.reported_usd ?? 0} unreported={f.cost.unreported_attempts ?? 0} what="this firing" />
                  )}
                </td>
                <td data-label="Largest run vs tripwire">
                  <LargestRun f={f} perRun={perRun} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * THE SCOPE BY `owner/repo`, AS THE LIST SAYS IT (visual QA V239): the head
 * printed the raw ids ("repo_a, repo_long…") where the list, one click away,
 * said "example-org/example-api". The registrations are read once for the
 * page; until they answer, or if that read fails, an id stands for itself.
 */
function ScopeRepos({ ids }: { ids: readonly string[] }) {
  const [names, setNames] = useState<ReadonlyMap<string, string> | null>(null)
  useEffect(() => {
    let live = true
    void loadRepositories().then((r: Result<RepoRecord[]>) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setNames(new Map(r.data.map((x) => [x.repo_id, `${x.owner}/${x.repo}`])))
    })
    return () => {
      live = false
    }
  }, [])
  if (ids.length === 0) return <>no repository</>
  return (
    <>
      {ids.map((id, i) => (
        <Fragment key={id}>
          {i > 0 && ', '}
          <span data-repo={id} title={id}>
            {slashBreaks(names?.get(id) ?? id)}
          </span>
        </Fragment>
      ))}
    </>
  )
}

/** One fact of a tab: a name and its value. */
function Fact({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="sc-fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  )
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

/**
 * What the merge switch knows of the type's floor: `auto` allowed, never, not
 * read, or `off` -- a type that never merges at all (QA2 automate2-10), whose
 * schedule is never told to edit its gate to approve merges.
 */
type Floor = 'auto' | 'never' | 'off' | 'unknown' | 'reading'

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
      setFloor(row === undefined ? 'unknown' : row.floor_gate.merge === 'auto' ? 'auto' : row.floor_gate.merge === 'off' ? 'off' : 'never')
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
 * WHY THIS VIEWER CANNOT SWITCH, AND WHO CAN, or null while that is not known
 * or the viewer can. Two rules refuse before the server is asked:
 *
 *   * `second_person`: in a group tenant, the member who last changed the
 *     gate, scope or spec (AUT-4). A personal tenant types the name instead.
 *   * `platform_admin`: a schedule scoped to the platform, for a viewer who is
 *     not a platform admin (`merge_mode`'s "only admins in the platform
 *     repos"). A repos scope that happens to reach a platform repository is
 *     not knowable here; the server still refuses that, said in place.
 *
 * WHO CAN is the platform admins by name when this viewer can read them --
 * Admin › People's `admins`, which only an admin may read -- and otherwise a
 * pointer to that page. No member list is readable from the console, so the
 * other members are named by their tenant. No new route is asked for.
 */
export interface SwitchBlock {
  rule: 'second_person' | 'platform_admin'
  /** The tenant's display name, its group, or its id. */
  tenant: string
  /** The platform admins other than this viewer, or null when not readable here. */
  admins: string[] | null
}

function useSwitchBlock(s: Schedule, ask: boolean): SwitchBlock | null {
  const [block, setBlock] = useState<SwitchBlock | null>(null)
  useEffect(() => {
    if (!ask) return
    let live = true
    void Promise.all([loadMe({ frame: true }), loadScheduleAudit(s.schedule_id)]).then(async ([me, audit]) => {
      if (!live || (me.status !== 'ok' && me.status !== 'stale')) return
      const { tenant, principal } = me.data
      const self = principal.email.trim().toLowerCase()
      let rule: SwitchBlock['rule'] | null = null
      if (s.scope.mode === 'platform') {
        rule = principal.is_admin ? null : 'platform_admin'
      } else if (tenant.kind === 'group') {
        const entries = audit.status === 'ok' || audit.status === 'stale' ? audit.data.audit : audit.status === 'empty' ? [] : null
        if (entries === null) return
        const editor = lastGateEditor(entries, s)
        rule = editor !== null && editor === self ? 'second_person' : null
      }
      if (rule === null) return setBlock(null)
      const name = tenant.display_name || tenant.principal || tenant.tenant_id || 'this workspace'
      setBlock({ rule, tenant: name, admins: null })
      if (!principal.is_admin) return
      const people = await loadPeople()
      // A platform admin's read: only it carries `admins` (a workspace admin's is scoped, ADMIN-PEOPLE-ROLES).
      if (!live || (people.status !== 'ok' && people.status !== 'stale') || !('admins' in people.data) || !Array.isArray(people.data.admins)) return
      const admins = people.data.admins.map((a) => a.email).filter((e) => e.trim().toLowerCase() !== self)
      if (admins.length > 0) setBlock({ rule, tenant: name, admins })
    })
    return () => {
      live = false
    }
  }, [ask, s])
  return block
}

const BLOCK_REASON: Record<SwitchBlock['rule'], string> = {
  second_person: 'You last changed this schedule’s gate, scope or spec, so another member of this workspace must make this switch.',
  platform_admin: 'This schedule reaches a platform repository, where only a platform admin may switch merging to automatic.',
}

/** This schedule's Gate tab as a full URL, for sending to whoever can switch it. */
function gateLink(s: Schedule): string {
  const path = addressToPath(scheduleAddress(s.schedule_id, { tab: 'gate' }))
  return typeof window === 'undefined' ? path : `${window.location.origin}${path}`
}

/**
 * THE BLOCKED STATE, UP FRONT (owner review 2026-10-11). The dialog used to
 * open on two paragraphs of rules and say the one thing that mattered -- this
 * viewer cannot switch -- in grey under a button, with no word on who could.
 * Now it opens with that: the warning, the reason in one sentence, who can,
 * and a Copy link to send them.
 */
function BlockedSwitch({ id, s, block, go }: { id: string; s: Schedule; block: SwitchBlock; go?: (to: string) => void }) {
  const [said, setSaid] = useState('')
  const people =
    go === undefined ? (
      <a className="ctl-link" href={addressToPath('admin/people')}>Admin › People</a>
    ) : (
      <InApp go={go} to="admin/people" className="ctl-link">Admin › People</InApp>
    )
  const admins = block.admins !== null && block.admins.length > 0 ? block.admins.join(', ') : null
  const copy = () => {
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined || typeof clip.writeText !== 'function') return setSaid(`Copy refused: the link is ${gateLink(s)}`)
    clip.writeText(gateLink(s)).then(
      () => setSaid('Link copied'),
      () => setSaid(`Copy refused: the link is ${gateLink(s)}`),
    )
  }
  return (
    <div className="c-banner is-warn sc-blocked" id={id} data-testid="sc-switch-blocked">
      <WarnGlyph />
      <div className="c-banner-body">
        <h3>You can’t make this switch</h3>
        <p>{BLOCK_REASON[block.rule]}</p>
        {block.rule === 'second_person' ? (
          admins !== null ? (
            <p>Another member of {block.tenant} can switch it, or a platform admin: {admins}.</p>
          ) : (
            <p>Another member of {block.tenant} can switch it, or a platform admin of this workspace ({people} lists them).</p>
          )
        ) : admins !== null ? (
          <p>A platform admin must switch it: {admins}.</p>
        ) : (
          <p>A platform admin of this workspace must switch it ({people} lists them).</p>
        )}
        {said !== '' && <p role="status">{said}</p>}
      </div>
      <div className="c-banner-acts">
        <Button size="sm" icon={<CIcon name="copy" />} onClick={copy}>
          Copy link
        </Button>
      </div>
    </div>
  )
}

/**
 * THE SD3 MERGE SWITCH (§4.2, owner decision 2026-10-08). `approve` to `auto`
 * is the one way a schedule merges unattended: never the create or edit form,
 * always this audited route, and never by the member who last changed the
 * gate, scope or spec. The server holds every rule and says which refused;
 * the dialog also reads who may not switch and opens on that (AUT-4, owner
 * review 2026-10-11), rather than offering a button the server will refuse.
 *
 * ONE SHAPE, BLOCKED OR NOT: the warning when blocked, then a short "What
 * changes", the full rules behind Details, and one action row -- Close, then
 * the primary. Blocked, the primary is disabled (aria-disabled too), grey,
 * and reads "Switch to automatic" with no ellipsis: nothing follows a click.
 * Its reason is the warning above, said once, not again beside the button,
 * where the wrapped reason is what pushed Close and Switch onto two rows.
 *
 * TWO STEPS (AUT-4). The first says what changes and what still holds; the
 * second asks for the schedule's name typed, and only then sends. The typed
 * name rides as `confirm`, which is what a one-person tenant's switch needs
 * (422 `confirmation_required`, SD5's exception) and which the route ignores
 * elsewhere. Back to `approve` is a tightening, open to any member, and needs
 * no dialog. Closing the dialog returns focus to its trigger (AUT-13).
 */
export function MergeSwitch({ s, onChanged, go }: { s: Schedule; onChanged: () => void; go?: (to: string) => void }) {
  const floor = useMergeFloor(s.type)
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState<'explain' | 'type'>('explain')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const trigger = useRef<HTMLSpanElement | null>(null)
  const blockId = useId()
  const typedHint = useId()
  const merge = s.gate.merge
  const blocked = useSwitchBlock(s, merge === 'approve' && floor !== 'never' && floor !== 'off')

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
        This schedule does not merge.
        {floor === 'off'
          ? ' Its type never merges, so there is no gate to edit.'
          : floor !== 'reading' && ' Edit its gate to approve each merge before it can be switched to automatic.'}
      </p>
    )
  }
  if (floor === 'reading') return <p className="sb-note" data-testid="sc-merge-switch">Reading whether this type may merge unattended…</p>
  if (floor === 'never' || floor === 'off') {
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
                <Button onClick={close}>Close</Button>
                {blocked !== null ? (
                  <Button kind="primary" className="sc-switch-go" disabled aria-disabled="true" aria-describedby={blockId}>
                    Switch to automatic
                  </Button>
                ) : (
                  <Button kind="primary" className="sc-switch-go" onClick={() => { setStep('type'); setRefused(null) }}>
                    Switch to automatic merging
                  </Button>
                )}
              </>
            ) : (
              <>
                <Button onClick={() => setStep('explain')}>Back</Button>
                <Button
                  kind="primary"
                  className="sc-switch-go"
                  busy={busy ? 'Switching…' : false}
                  disabled={blocked !== null || !typedOk}
                  aria-disabled={blocked !== null || !typedOk ? 'true' : undefined}
                  aria-describedby={typedHint}
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
              {blocked !== null && <BlockedSwitch id={blockId} s={s} block={blocked} go={go} />}
              <h3 className="sc-h3">What changes</h3>
              <ul className="sc-changes">
                <li>A pull request this schedule makes merges once it is green and reviewed, with no person approving it.</li>
                <li>
                  Still held: workflow files; IAM, terraform/bootstrap, CODEOWNERS and protected paths; and the frozen contract.
                </li>
              </ul>
              <details className="sc-rules">
                <summary>Details</summary>
                <p>
                  The hard stops still hold whatever this says: a plan naming workflow files is refused; IAM, terraform/bootstrap,
                  CODEOWNERS and the repository’s protected paths are held for a second member to approve (for the owner, in a platform
                  repository); and in a platform repository the frozen contract is held for the owner.
                </p>
                <p>
                  The member who last changed this schedule’s gate, scope or spec cannot make this switch; in a platform repository only a
                  platform admin can. The switch is written to the schedule’s audit.
                </p>
              </details>
            </>
          ) : (
            <label className="c-field">
              <span className="c-lbl">
                Type <code>{s.name}</code> to switch merging to automatic. In a workspace of one person, this typed name stands in for the
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
              <span className="c-hint" id={typedHint}>
                {typedOk ? 'The name matches.' : 'Type the schedule’s name exactly.'}
              </span>
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

/**
 * ONE PARAMETER, READABLE (visual QA V171). A custom-prompt schedule's `spec`
 * was one minified `JSON.stringify` line in a 190px fact column: ~840px of
 * wrapped mono with `\n` escapes, the prompt in it unreadable. A value with
 * structure is its own full-width fact, one row per key, and its text keeps
 * its line breaks.
 */
function ParamValue({ v }: { v: unknown }) {
  const plain = plainValue(v)
  if (plain !== null) return <span className={typeof v === 'string' && v.includes('\n') ? 'sc-ptext' : undefined}>{plain}</span>
  if (Array.isArray(v)) {
    return (
      <ol className="sc-pobj">
        {v.map((x, i) => (
          <li key={i}>
            <ParamValue v={x} />
          </li>
        ))}
      </ol>
    )
  }
  return (
    <dl className="sc-pobj">
      {Object.entries(v as Record<string, unknown>).map(([k, x]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>
            <ParamValue v={x} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Settings({ s }: { s: Schedule }) {
  const params = Object.entries(s.params)
  return (
    <dl className="sc-facts">
      {params.length === 0 ? (
        <Fact name="Parameters">the type's defaults</Fact>
      ) : (
        params.map(([k, v]) =>
          plainValue(v) === null ? (
            <div key={k} className="sc-fact sc-wide" data-param={k}>
              <dt>{k}</dt>
              <dd>
                <ParamValue v={v} />
              </dd>
            </div>
          ) : (
            <Fact key={k} name={k}>
              <ParamValue v={v} />
            </Fact>
          ),
        )
      )}
      <Fact name="Overlap">{s.policy.overlap ?? '— not set'}</Fact>
      <Fact name="Catch-up">{s.policy.catch_up ?? '— not set'}</Fact>
      <Fact name="Jitter">{s.policy.jitter === undefined ? '— not set' : s.policy.jitter ? 'on' : 'off'}</Fact>
      <Fact name="Dry run">{s.policy.dry_run === undefined ? '— not set' : s.policy.dry_run ? 'on' : 'off'}</Fact>
      <Fact name="Revision">{s.revision}</Fact>
    </dl>
  )
}

/**
 * AN EDIT'S CHANGES, ONE LINE EACH (visual QA V243): the route records
 * `changed` as `{field: {from, to}}` with whole field values, and it was
 * printed as raw JSON. A field whose value is an object (the gate, the
 * budget, the policy) names the keys that moved; the rest say from → to.
 */
export function changeLines(changed: unknown): string[] {
  if (changed === null || typeof changed !== 'object') return []
  const said = (v: unknown) => plainValue(v) ?? JSON.stringify(v)
  const lines: string[] = []
  for (const [field, c] of Object.entries(changed as Record<string, unknown>)) {
    const { from, to } = (c ?? {}) as { from?: unknown; to?: unknown }
    const objects = [from, to].every((x) => x === null || x === undefined || (typeof x === 'object' && !Array.isArray(x)))
    if (objects && (from ?? to) !== null && (from ?? to) !== undefined) {
      const a = (from ?? {}) as Record<string, unknown>
      const b = (to ?? {}) as Record<string, unknown>
      const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k]))
      for (const k of keys) lines.push(`${field} · ${k}: ${said(a[k])} → ${said(b[k])}`)
      continue
    }
    lines.push(`${field}: ${said(from)} → ${said(to)}`)
  }
  return lines
}

function Audit({ scheduleId }: { scheduleId: string }) {
  const [r, setR] = useState<Result<{ audit: ScheduleAuditEntry[] }>>({ status: 'loading', since: Date.now() })
  const [round, setRound] = useState(0)
  useEffect(() => {
    let live = true
    setR({ status: 'loading', since: Date.now() })
    void loadScheduleAudit(scheduleId).then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [scheduleId, round])
  const now = useNow()
  if (r.status === 'loading') return <p className="sb-note">Reading the audit…</p>
  // THE FAILURE PANEL, NOT A BARE MARK (visual QA V242): "not read · Audit not
  // read" gave no reason, no HTTP code and no way to read it again.
  if (r.status === 'error') return <FailedPanel error={r.error} onRetry={() => setRound((n) => n + 1)} />
  if (r.status === 'empty') {
    return <Absent kind="zero" heading="No audit entries" say="The audit was read and holds no entry for this schedule." />
  }
  return (
    <ol className="sc-audit">
      {r.data.audit.map((e, i) => {
        const changes = changeLines((e.detail as { changed?: unknown }).changed)
        const detail = Object.entries(e.detail)
          .filter(([k]) => k !== 'changed')
          .map(([k, v]) => `${k} ${plainValue(v) ?? JSON.stringify(v)}`)
          .join(' · ')
        return (
          <li key={`${e.at}:${i}`} data-audit={e.action}>
            <span className="sc-who">{e.by}</span> <b>{e.action.replace(/_/g, ' ')}</b>
            {detail !== '' && <span className="sc-sub sc-adetail">{detail}</span>}
            {changes.length > 0 && (
              <ul className="sc-achanges" aria-label="What changed">
                {changes.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
            <span className="sc-sub" title={localTime(e.at)}>{timeAgo(e.at, now)}</span>
          </li>
        )
      })}
    </ol>
  )
}

// ---------------------------------------------------------------------------
// The sheet (Automate proposal A, frame A2; AUTO-A2, part of #2417)
// ---------------------------------------------------------------------------

/** How many firings the sheet draws at a time, of the last 50 served (frame A2: "ten drawn"). */
export const SHEET_FIRINGS = 10

/** The folds at the sheet's foot; either is named by the address's `tab=`, so a link lands on it open. */
type Fold = 'settings' | 'audit'

const RUN_GATE: Readonly<Record<string, string>> = { auto: 'starts by itself', approve: 'waits for an approval' }
const PLAN_GATE: Readonly<Record<string, string>> = { auto: 'goes ahead by itself', approve: 'waits for an approval' }
const MERGE_GATE: Readonly<Record<string, string>> = {
  off: 'off: a pull request is left for a person to merge',
  approve: 'waits for an approval at the green head',
  auto: 'merges by itself once it is green',
}

/**
 * WHAT A GATE THE SCHEDULE LEAVES UNSET FALLS BACK TO (`schedules.resolve_gate`):
 * the type's `default_gate` for run, plan and merge, `members` for who
 * approves, and `APPROVAL_TTL_DEFAULT_HOURS` for the expiry. The sheet says
 * what applies, marked "(default)" (QA1 automate-20): "— not set" beside a
 * gate that waits for an approval read as if nobody could approve it.
 */
const APPROVAL_TTL_DEFAULT_HOURS = 72

/** Who a schedule's `gate.approvers` admits (`schedules.GateIn`). */
function approversWords(a: unknown): string {
  if (a === undefined || a === null || a === '') return 'any member of this workspace (default)'
  if (a === 'members') return 'any member of this workspace'
  if (a === 'owner_only') return 'the platform owner only'
  if (Array.isArray(a)) return a.map(String).join(', ')
  return String(a)
}

const OVERLAP_WORDS: Readonly<Record<string, string>> = {
  skip: 'A firing while the last one is still running is skipped.',
  queue_one: 'A firing while the last one is still running waits for it, one at most.',
}
const CATCH_UP_WORDS: Readonly<Record<string, string>> = {
  skip: 'A slot missed in an outage is skipped.',
  run_once: 'Slots missed in an outage fire once, when the platform is back.',
}

/** The policy's three cadence rules in words, each only when it is set. */
function cadenceRules(s: Schedule): string[] {
  const p = s.policy
  const out: string[] = []
  if (p.jitter === true) out.push('Each firing starts a little after its slot (jitter): at most five minutes, and at most a tenth of the interval.')
  if (p.jitter === false) out.push('Each firing starts at its slot.')
  if (p.catch_up !== undefined) out.push(CATCH_UP_WORDS[p.catch_up] ?? `Catch-up: ${p.catch_up}.`)
  if (p.overlap !== undefined) out.push(OVERLAP_WORDS[p.overlap] ?? `Overlap: ${p.overlap}.`)
  return out
}

/** One name and its value in the sheet's two-column facts. */
function Kv({ name, children }: { name: string; children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  )
}

/** One section of the sheet: a ruled heading and what it says. */
function Sec({ name, title, sub, children }: { name: string; title: string; sub?: ReactNode; children: ReactNode }) {
  const id = useId()
  return (
    <section className="as-sec" data-sec={name} aria-labelledby={id}>
      <h3 id={id}>
        {title}
        {sub !== undefined && <small>{sub}</small>}
      </h3>
      {children}
    </section>
  )
}

/** What the type does, from the catalogue (`GET /v1/schedule-types`); the type's name stands in until it answers. */
function useTypeRow(type: string): ScheduleTypeRow | null | 'unread' {
  const [row, setRow] = useState<ScheduleTypeRow | null | 'unread'>(null)
  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (!live) return
      if (r.status !== 'ok' && r.status !== 'stale') return setRow('unread')
      setRow(r.data.types.find((t) => t.name === type) ?? 'unread')
    })
    return () => {
      live = false
    }
  }, [type])
  return row
}

/**
 * THE NEXT FIVE FIRINGS from the tick's own parser (`POST
 * /v1/schedules:preview`, the read the editor's preview makes). Only for a
 * schedule that fires: a paused one has none, and says so in its word.
 */
function useNextFirings(s: Schedule, fires: boolean): string[] | 'reading' | 'unread' | null {
  const [next, setNext] = useState<string[] | 'reading' | 'unread' | null>(fires ? 'reading' : null)
  useEffect(() => {
    if (!fires) return setNext(null)
    let live = true
    setNext('reading')
    void previewSchedule({ cron: s.cron, timezone: s.timezone, type: s.type }).then((r) => {
      if (!live) return
      setNext(r.status === 'ok' || r.status === 'stale' ? r.data.preview.next : 'unread')
    })
    return () => {
      live = false
    }
  }, [s.cron, s.timezone, s.type, fires])
  return next
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * A slot as its own wall time: the preview serves each in the schedule's zone
 * with its offset, and the section's heading names the zone, so "Oct 28
 * 02:00" is read off the string, never moved to the reader's zone. The
 * reader's own time is the title.
 */
export function slotWords(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (m === null) return iso
  return `${MONTHS[Number(m[2]) - 1] ?? m[2]} ${Number(m[3])} ${m[4]}:${m[5]}`
}

function Cadence({ s, word }: { s: Schedule; word: StateWord | undefined }) {
  const now = useNow()
  const fires = s.state === 'enabled'
  const next = useNextFirings(s, fires)
  const rules = cadenceRules(s)
  return (
    <Sec
      name="cadence"
      title="Cadence"
      sub={
        <>
          {s.words ?? 'the tick could not read this cron'} · <code>{s.cron}</code> · {s.timezone}
        </>
      }
    >
      {!fires ? (
        <p className="as-none" data-testid="as-next">
          <i className="ctl-em">&mdash; {word === undefined ? STATE_WORD[s.state] : STATE_WORD_SAID[word]}, so no next firing</i>
        </p>
      ) : next === 'reading' ? (
        <p className="sb-note">Reading the next firings…</p>
      ) : next === 'unread' || next === null || next.length === 0 ? (
        <p className="as-none" data-testid="as-next">
          {s.next_run_at === null ? (
            <i className="ctl-em">&mdash; the next firings could not be read</i>
          ) : (
            <>
              Next <b title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</b>
              <span className="sc-sub">the firings after it could not be read</span>
            </>
          )}
        </p>
      ) : (
        <>
          {/* WHICH CLOCK, beside the chips (QA1 automate-14), in the form's words ("Next five, in UTC"): the zone in the heading is a line away. */}
          <p className="as-note" data-testid="as-next-zone">
            Next {next.length === 5 ? 'five' : next.length}, in {s.timezone}:
          </p>
          <ol className="as-fires" data-testid="as-next" aria-label={`The next firings, in ${s.timezone}`}>
            {next.map((at) => (
              <li key={at} title={localTime(at)}>
                {slotWords(at)}
                {s.timezone === 'UTC' ? 'Z' : ''}
              </li>
            ))}
          </ol>
        </>
      )}
      {rules.length > 0 && <p className="as-note">{rules.join(' ')}</p>}
    </Sec>
  )
}

function Firings({ s, firings, go }: { s: Schedule; firings: ServedFiring[]; go: (to: string) => void }) {
  const [page, setPage] = useState(0)
  const [view, setView] = useState<'table' | 'timeline'>('table')
  const now = useNow()
  if (firings.length === 0 && s.last_firing !== null) {
    // FIRED, BUT NOT SERVED (QA2 automate2-02): an empty list beside a
    // `last_firing` is a read that served no firings, never "Never fired" --
    // the list's Last firing column says the same schedule failed 5h ago.
    const last = s.last_firing
    const at = last.ended_at ?? last.slot
    return (
      <Sec name="firings" title="Firings">
        <p className="sb-note" data-testid="sc-firings-unserved">
          &mdash; recent firings not served by this read. The last firing{' '}
          {last.outcome === null ? 'is still in progress' : <b>{last.outcome}</b>}
          {at !== null && (
            <>
              {', '}
              <span title={localTime(at)}>{timeAgo(at, now)}</span>
            </>
          )}
          .
        </p>
      </Sec>
    )
  }
  if (firings.length === 0) {
    // A6: NEVER FIRED IS A FACT, NOT A ZERO (§6.2). No table, no "0 runs".
    return (
      <Sec name="firings" title="Firings">
        <Absent kind="zero" heading="Never fired" say="This schedule has no firing recorded: it has not fired yet.">
          {s.next_run_at === null ? 'Nothing is planned while it is paused.' : `Its first firing is ${localTime(s.next_run_at)}.`}
        </Absent>
      </Sec>
    )
  }
  const pages = Math.ceil(firings.length / SHEET_FIRINGS)
  const at = Math.min(page, pages - 1)
  const shown = firings.slice(at * SHEET_FIRINGS, (at + 1) * SHEET_FIRINGS)
  return (
    <Sec name="firings" title="Firings" sub={`last ${firings.length} served · ${shown.length} drawn, newest first`}>
      {view === 'table' ? (
        <FiringRows firings={shown} perRun={s.budget.per_run_usd} go={go} />
      ) : (
        <HistoryTimeline scheduleId={s.schedule_id} firings={firings as ScheduleFiring[]} go={go} outcome={(f) => <FiringOutcome f={f} />} />
      )}
      <div className="as-pager">
        {view === 'table' && pages > 1 && (
          <>
            <Button size="sm" disabled={at === 0} onClick={() => setPage(at - 1)}>
              ‹ Newer
            </Button>
            <span className="as-dim">
              firings {at * SHEET_FIRINGS + 1}–{at * SHEET_FIRINGS + shown.length} of {firings.length}
            </span>
            <Button size="sm" disabled={at === pages - 1} onClick={() => setPage(at + 1)}>
              Older ›
            </Button>
          </>
        )}
        <Button size="sm" className="as-view" aria-pressed={view === 'timeline'} onClick={() => setView(view === 'table' ? 'timeline' : 'table')}>
          {view === 'table' ? 'Timeline' : 'Table'}
        </Button>
      </div>
    </Sec>
  )
}

/**
 * SPEND TODAY (frame A2): the day bar is `budget_use`'s four parts
 * (`BurnBar`, which adds nothing up), then the day budget and the per-run
 * tripwire as the tick holds them.
 */
function Spend({ s, type }: { s: Schedule; type: ScheduleTypeRow | null | 'unread' }) {
  const b = s.budget
  // A FIELD LEFT TO THE TYPE (QA2 automate2-18): the gate rows' words, not
  // "— not set". `resolve_budget` fills an unset field from the type's caps.
  const left = (key: 'per_day_usd' | 'per_run_usd' | 'max_concurrent', say: (v: number) => string): ReactNode => {
    if (type === null) return <span className="as-dim">the type’s default; reading which…</span>
    const d = type === 'unread' ? undefined : type.budget_caps?.[key]
    if (d === undefined || d === null) return <i className="ctl-em">&mdash; the type’s default, which the catalogue did not say</i>
    return `${say(d)} (the type’s default)`
  }
  return (
    <Sec name="spend" title="Spend today" sub={`${dayWords(s.spend_today.day)} · the schedule’s own day`}>
      {s.budget_use === undefined || s.budget_use === null ? (
        <p className="sb-note" data-testid="sc-burn-absent">
          {s.budget_use === null
            ? '— no day bar: the budget has no per-run or per-day figure to count against.'
            : '— no day bar: this read did not serve today’s reservation.'}{' '}
          Spent so far:{' '}
          <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
        </p>
      ) : (
        <BurnBar use={s.budget_use} />
      )}
      <dl className="as-kv" data-testid="sc-budget">
        <Kv name="Day budget">
          {b.per_day_usd === undefined ? (
            left('per_day_usd', (v) => `${usd(v)} a day`)
          ) : (
            <>
              <b>{usd(b.per_day_usd)}</b> a day. When it is spent, the next firing is skipped.
            </>
          )}
        </Kv>
        <Kv name="Per-run tripwire">
          {b.per_run_usd === undefined ? (
            left('per_run_usd', (v) => `${usd(v)} a run`)
          ) : (
            <>
              <b>{usd(b.per_run_usd)}</b> a run. A run that costs more fails its firing and pauses this schedule.
            </>
          )}
        </Kv>
        <Kv name="Live runs">{b.max_concurrent === undefined ? left('max_concurrent', (v) => `at most ${v} at once`) : `at most ${b.max_concurrent} at once`}</Kv>
      </dl>
    </Sec>
  )
}

function Gate({ s, type, onChanged, go }: { s: Schedule; type: ScheduleTypeRow | null | 'unread'; onChanged: () => void; go: (to: string) => void }) {
  const g = s.gate
  const failures = failuresToPause(s)
  const word = (map: Readonly<Record<string, string>>, v: string | undefined, point: 'run' | 'plan' | 'merge'): ReactNode => {
    if (v !== undefined) return map[v] ?? v
    if (type === null) return <span className="as-dim">the type’s default; reading which…</span>
    const d = type === 'unread' ? undefined : type.default_gate?.[point]
    if (d === undefined) return <i className="ctl-em">&mdash; the type’s default, which the catalogue did not say</i>
    return `${map[d] ?? d} (the type’s default)`
  }
  return (
    <Sec name="gate" title="Gates and approvers">
      <dl className="as-kv" data-testid="sc-gate">
        <Kv name="Run">{word(RUN_GATE, g.run, 'run')}</Kv>
        <Kv name="Plan">{word(PLAN_GATE, g.plan, 'plan')}</Kv>
        <Kv name="Merge">{g.merge === 'approve' ? <b>{MERGE_GATE.approve}</b> : word(MERGE_GATE, g.merge, 'merge')}</Kv>
        <Kv name="Who approves">{approversWords(g.approvers)}</Kv>
        <Kv name="Expiry">
          an approval nobody decides expires after {g.approval_ttl_hours ?? APPROVAL_TTL_DEFAULT_HOURS} h
          {g.approval_ttl_hours === undefined ? ' (default)' : ''}
        </Kv>
        <Kv name="Waiting">
          {s.pending_approvals === 0 ? (
            'nothing waits on a person'
          ) : (
            <>
              {pluralise(s.pending_approvals, 'approval')} waiting ·{' '}
              <InApp go={go} to={APPROVALS_ADDRESS} className="ctl-link">
                Open Approvals
              </InApp>
            </>
          )}
        </Kv>
        <Kv name="Hard stops">
          {/* The same sentence as the form's Gates card (QA1 automate-09). */}
          <HardStops />{' '}
          {failures === 1 ? 'A failed run pauses' : `${failures} failed runs in a row pause`} the schedule.
        </Kv>
      </dl>
      <h4 className="as-h4">Merging</h4>
      <MergeSwitch s={s} onChanged={onChanged} go={go} />
    </Sec>
  )
}

/** Settings or Audit, folded at the sheet's foot; opened, it is drawn in place (frame A2). */
function FoldSec({
  name,
  title,
  folded,
  open,
  onToggle,
  children,
}: {
  name: Fold
  title: string
  folded: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const id = useId()
  return (
    <section className="as-sec as-fold" data-sec={name} data-open={open ? 'open' : 'shut'}>
      <h3>
        <button type="button" className="as-fold-b" aria-expanded={open} aria-controls={id} onClick={onToggle}>
          <span>{title}</span>
          <CIcon name={open ? 'up' : 'down'} />
        </button>
      </h3>
      {open ? (
        <div id={id} className="as-fold-body">
          {children}
        </div>
      ) : (
        <p id={id} className="as-note">
          {folded}
        </p>
      )}
    </section>
  )
}

function SheetBody({
  schedule: s,
  firings,
  go,
  fold,
  reread,
  notice,
  onDone,
}: {
  schedule: Schedule & { state_word?: StateWord }
  firings: ServedFiring[]
  go: (to: string) => void
  fold: Fold | null
  reread: () => void
  notice: ActNotice | null
  onDone: (notice: ActNotice) => void
}) {
  const type = useTypeRow(s.type)
  const now = useNow()
  // THE FOLD IS IN THE ADDRESS (AUT-11): a reload, Back and a pasted link land with it open.
  const toggle = (f: Fold) => go(scheduleAddress(s.schedule_id, fold === f ? {} : { tab: f }))
  const edited = s.updated_at && s.updated_by ? `; edited ${timeAgo(s.updated_at, now)} by ${s.updated_by}` : ''
  return (
    <>
      <div className="as-acts" data-testid="as-acts">
        <ScheduleActions s={s} onDone={onDone} onDeleted={() => go(SCHEDULES_ADDRESS)} />
        <Button onClick={() => go(scheduleAddress(s.schedule_id, { edit: true }))}>Edit</Button>
      </div>
      <Said notice={notice} />
      <Sec name="what" title="What it does">
        <p data-testid="as-what">
          {type === null ? <span className="as-dim">Reading what {s.type} does…</span> : type === 'unread' ? (
            <i className="ctl-em">&mdash; what {s.type} does could not be read from the catalogue</i>
          ) : (
            type.description
          )}
        </p>
        <dl className="as-kv">
          <Kv name="Type">
            {s.type}
            {s.tier === null ? ' · the type has left the catalogue' : ` · risk tier ${s.tier}`}
          </Kv>
          <Kv name="Target">
            {s.scope.mode === 'all' ? 'every registered repository' : s.scope.mode === 'platform' ? 'the platform' : <ScopeRepos ids={s.scope.repo_ids ?? []} />}
          </Kv>
          <Kv name="Owner">
            {s.owner} · <span title={localTime(s.created_at)}>created {timeAgo(s.created_at, now)}</span> by {s.created_by}
            {edited}
          </Kv>
          {s.pause !== null && s.state !== 'enabled' && (s.pause.reason || s.pause.code) && (
            <Kv name="Why paused">
              {s.pause.reason || s.pause.code}
              {s.pause.by ? ` · ${s.pause.by}` : ''}
              {s.pause.at ? `, ${timeAgo(s.pause.at, now)}` : ''}
            </Kv>
          )}
        </dl>
      </Sec>
      <Cadence s={s} word={s.state_word} />
      <Firings s={s} firings={firings} go={go} />
      <Spend s={s} type={type} />
      <Gate s={s} type={type} onChanged={reread} go={go} />
      <FoldSec
        name="settings"
        title="Settings"
        folded={`Parameters, overlap, catch-up, jitter and dry run; revision ${s.revision}.`}
        open={fold === 'settings'}
        onToggle={() => toggle('settings')}
      >
        <Settings s={s} />
      </FoldSec>
      <FoldSec
        name="audit"
        title="Audit"
        folded="Every change to this schedule, who made it and when, newest first. Read when opened."
        open={fold === 'audit'}
        onToggle={() => toggle('audit')}
      >
        <Audit scheduleId={s.schedule_id} />
      </FoldSec>
    </>
  )
}

type Read = Result<{ schedule: Schedule & { state_word?: StateWord }; firings: ServedFiring[] }>

/**
 * ONE SCHEDULE IN THE SIDE SHEET OVER THE LIST (frame A2). Its own read, so
 * the list beside it keeps its rows and its age; a write (an action, the
 * merge switch) reads the schedule again rather than patching what it drew.
 * Escape and the close button go back to the list; on a phone the sheet is
 * the page and `‹ Schedules` goes back (frame A7b).
 */
function ScheduleSheet({ scheduleId, go, fold }: { scheduleId: string; go: (to: string) => void; fold: Fold | null }) {
  const [r, setR] = useState<Read>({ status: 'loading', since: Date.now() })
  const [round, setRound] = useState(0)
  const [notice, setNotice] = useState<ActNotice | null>(null)
  // A 404 AFTER A READ THAT FOUND IT IS A DELETION (AUT-9), not a read failure.
  const seen = useRef(false)
  const [gone, setGone] = useState(false)
  const headingId = useId()
  const sheet = useRef<HTMLElement | null>(null)
  useEffect(() => {
    let live = true
    void loadSchedule(scheduleId).then((next) => {
      if (!live) return
      if (next.status === 'ok' || next.status === 'stale') seen.current = true
      else if (next.status === 'error' && next.error.kind === 'not_found' && seen.current) setGone(true)
      setR(next as Read)
    })
    return () => {
      live = false
    }
  }, [scheduleId, round])
  // OPENED, IT STARTS AT ITS TOP WITH FOCUS ON ITS HEADING, so a screen
  // reader says which schedule opened and Tab and Escape start inside it. Not
  // on Close or `‹ Schedules` (QA1 automate-03): the first of those in the
  // page is `‹ Schedules`, which a desktop does not draw, so the focus went
  // nowhere and the shell put it on the list's own heading behind the sheet.
  useEffect(() => {
    const el = sheet.current
    if (el === null) return
    el.scrollTop = 0
    if (!el.contains(document.activeElement)) el.querySelector<HTMLElement>('.as-head h1')?.focus({ preventScroll: true })
  }, [scheduleId])
  // CLOSED, IT HANDS FOCUS BACK TO ITS ROW in the list (`sheetClosed`).
  const close = () => {
    sheetClosed(scheduleId)
    go(SCHEDULES_ADDRESS)
  }
  const closeRef = useRef(close)
  closeRef.current = close
  // ESCAPE CLOSES THE SHEET wherever focus is, unless a dialog on it (Delete's typed confirmation, Run now's) has it first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || document.querySelector('.c-dlg') !== null) return
      closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  const s = r.status === 'ok' || r.status === 'stale' ? r.data.schedule : null
  const title = s?.name ?? scheduleId
  const word = s?.state_word
  return (
    <aside className="as-sheet" ref={sheet} aria-labelledby={headingId} data-schedule={scheduleId}>
      <InApp go={go} to={SCHEDULES_ADDRESS} className="as-back" onGo={() => sheetClosed(scheduleId)}>
        ‹ Schedules
      </InApp>
      <div className="as-head">
        <div className="as-title">
          <span className="as-crumb">Schedules ›</span>
          <h1 id={headingId} title={title} tabIndex={-1}>
            {title}
          </h1>
          {s !== null && !gone && (
            <p className="as-meta" data-testid="as-meta">
              {word === undefined ? (
                <i className="ctl-em" title="This API serves no state_word.">&mdash; not served</i>
              ) : (
                <ToneMark tone={WORD_TONE[word]} className="as-word">
                  {STATE_WORD_SAID[word]}
                </ToneMark>
              )}
              <DryRunChip s={s} />
              <span>{s.tier === null ? s.type : `${s.type} · ${s.tier}`}</span>
              <span className="mono">{s.schedule_id}</span>
            </p>
          )}
        </div>
        <Button className="as-close" size="sm" iconOnly icon={<CIcon name="close" />} onClick={close}>
          Close this schedule
        </Button>
      </div>
      {gone ? (
        <Absent kind="zero" heading="This schedule was deleted" say="It was read here before, and the schedule is no longer there: someone deleted it.">
          Its firings and audit are kept, and work it made carries on as ordinary work.
        </Absent>
      ) : r.status === 'loading' ? (
        <p className="sb-note">Reading the schedule…</p>
      ) : r.status === 'error' ? (
        r.error.kind === 'not_found' ? (
          <Absent kind="zero" heading="Not found" say="No schedule by this id is in this workspace.">
            It may be another workspace’s, or it was deleted before this page read it.
          </Absent>
        ) : (
          <FailedPanel error={r.error} onRetry={() => setRound((n) => n + 1)} />
        )
      ) : s === null ? (
        <Absent kind="zero" heading="Not found" say="No schedule by this id is in this workspace." />
      ) : (
        <SheetBody
          schedule={s}
          firings={(r as Extract<Read, { data: unknown }>).data.firings}
          go={go}
          fold={fold}
          reread={() => setRound((n) => n + 1)}
          notice={notice}
          onDone={(n) => {
            setNotice(n)
            setRound((k) => k + 1)
          }}
        />
      )}
    </aside>
  )
}

/** The editor's page (`edit=1`): the A3 form, drawn on its own page as before this sheet. */
function EditPage({ scheduleId, go, tab }: { scheduleId: string; go: (to: string) => void; tab: DetailTab }) {
  // A local, not a literal: the page is headed by the schedule's own name.
  const [name, setName] = useState<string | null>(null)
  const pageTitle = name ?? scheduleId
  const [round, setRound] = useState(0)
  const back = scheduleAddress(scheduleId, { tab })
  return (
    <div className="sc-detail">
      <InApp go={go} to={back} className="sc-back">‹ {pageTitle}</InApp>
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
          <EditSchedule
            schedule={d.schedule}
            onCancel={() => go(back)}
            onSaved={() => {
              go(back)
              setRound((n) => n + 1)
            }}
          />
        )}
      </Screen>
    </div>
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
  /** The fold the address names (`tab=settings` or `tab=audit`); any other tab opens none. */
  tab?: DetailTab
  /** The address names the editor (`/automate/schedules/<id>/edit`). */
  editing?: boolean
}) {
  if (editing) return <EditPage scheduleId={scheduleId} go={go} tab={tab} />
  return <ScheduleSheet scheduleId={scheduleId} go={go} fold={tab === 'settings' || tab === 'audit' ? tab : null} />
}
