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
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  loadMe,
  loadPeople,
  loadSchedule,
  loadScheduleAudit,
  loadScheduleTypes,
  loadRepositories,
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
import { Button, CIcon, Dialog, Tabs } from './components'
import { WarnGlyph } from './components/glyphs'
import type { ApiError, Result } from './fetch'
import { usd } from './measure'
import { pluralise } from './types'
import { Absent, Mark } from './primitives'
import { addressToPath } from './paths'
import { EditSchedule, plainValue } from './ScheduleEdit'
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
  slashBreaks,
  untilWords,
  type ActNotice,
} from './Schedules'
import { FailedPanel, Screen, timeAgo } from './Shell'
import type { RepoRecord } from './RepositoriesData'
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
  if (f.skip?.code === 'NOTHING_STARTED') return `nothing started: ${nothingStartedWords(nothingDetail(f))}`
  if (f.skip !== null) return `skipped: ${f.skip.code}`
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
      const name = tenant.display_name || tenant.principal || tenant.tenant_id || 'this tenant'
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
  second_person: 'You last changed this schedule’s gate, scope or spec, so another member of this tenant must make this switch.',
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
            <p>Another member of {block.tenant} can switch it, or a platform admin of this tenant ({people} lists them).</p>
          )
        ) : admins !== null ? (
          <p>A platform admin must switch it: {admins}.</p>
        ) : (
          <p>A platform admin of this tenant must switch it ({people} lists them).</p>
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

function Gate({ s, onChanged, go }: { s: Schedule; onChanged: () => void; go: (to: string) => void }) {
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
      <MergeSwitch s={s} onChanged={onChanged} go={go} />
    </div>
  )
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
      <div className="sc-head-row" data-testid="sc-head-row">
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
          {s.scope.mode === 'all' ? 'every registered repository' : s.scope.mode === 'platform' ? 'the platform' : <ScopeRepos ids={s.scope.repo_ids ?? []} />}
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
        {tab === 'gate' && <Gate s={s} onChanged={reread} go={go} />}
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
