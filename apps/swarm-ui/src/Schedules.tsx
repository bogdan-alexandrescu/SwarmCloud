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
import { useEffect, useState, type MouseEvent, type ReactNode } from 'react'
import { loadApprovals, loadRepositories, loadSchedules, type ApprovalItem, type Schedule } from './api'
import { errorHeading, type Result } from './fetch'
import { usd } from './measure'
import { addressToPath } from './paths'
import { Absent, Mark } from './primitives'
import { scheduleWords, type RepoRecord } from './RepositoriesData'
import { ScheduleDetail } from './ScheduleDetail'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

/** The list's address, and one schedule's (`/automate/schedules/<id>`, paths.ts). */
export const SCHEDULES_ADDRESS = 'automate/schedules'
export function scheduleAddress(id: string): string {
  return `${SCHEDULES_ADDRESS}?${new URLSearchParams({ schedule: id }).toString()}`
}

/** The schedule a Schedules address names (`schedule=<id>`), or null for the list. */
function scheduleOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('schedule')
  return id === null || id === '' ? null : id
}

/** A link inside the app: an href for a new tab, `go` for a plain click. */
export function InApp({ to, go, children, className }: { to: string; go: (to: string) => void; children: ReactNode; className?: string }) {
  return (
    <a
      href={addressToPath(to)}
      className={className}
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
        {usd(usdReported)} · {missing} unreported
      </span>
    )
  }
  return <span className="sc-spend">{usd(usdReported)}</span>
}

/** The cron in words, or the expression itself when the parser refused it. */
function WhenCell({ s }: { s: Schedule }) {
  return (
    <>
      <span className="sc-cut" title={`${s.cron} · ${s.timezone}`}>{s.words ?? s.cron}</span>
      <span className="sc-sub">{s.timezone}</span>
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

function ScheduleList({ rows, names, go }: { rows: Schedule[]; names: ReadonlyMap<string, string> | null; go: (to: string) => void }) {
  const now = useNow()
  return (
    <div className="sc-table-wrap">
      <table className="sc-table">
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
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const line = stateLine(s)
            return (
              <tr key={s.schedule_id} className="sc-row" data-schedule={s.schedule_id}>
                <td data-label="Name · type">
                  <InApp go={go} to={scheduleAddress(s.schedule_id)} className="sc-cut sc-name">{s.name}</InApp>
                  <span className="sc-sub">{s.tier === null ? s.type : `${s.type} · ${s.tier}`}</span>
                </td>
                <td data-label="Repository"><span className="sc-cut" title={scopeWords(s, names)}>{scopeWords(s, names)}</span></td>
                <td data-label="When"><WhenCell s={s} /></td>
                <td data-label="Next run">
                  {s.next_run_at === null ? (
                    <i className="ctl-em" title={NO_NEXT_RUN[s.state]}>&mdash; {NO_NEXT_RUN[s.state]}</i>
                  ) : (
                    <span title={localTime(s.next_run_at)}>{untilWords(s.next_run_at, now)}</span>
                  )}
                </td>
                <td data-label="Last run">
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
                <td data-label="Spend today">
                  <SpendFigure usdReported={s.spend_today.reported_usd} unreported={s.spend_today.unreported_attempts} what="today's runs" />
                </td>
                <td data-label="Approvals">{s.pending_approvals > 0 ? `${s.pending_approvals} waiting` : 'none'}</td>
                <td data-label="State">
                  <span className={`sc-state is-${s.state}`}>{STATE_WORD[s.state]}</span>
                  {line !== null && <span className="sc-sub sc-cut" title={line}>{line}</span>}
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
                <td data-label="Built in"><span className="sc-cut">Index · {r.owner}/{r.repo}</span></td>
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

function SchedulesBody({ rows, go }: { rows: Schedule[]; go: (to: string) => void }) {
  const [repos, setRepos] = useState<Result<RepoRecord[]>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadRepositories().then((r) => {
      if (live) setRepos(r)
    })
    return () => {
      live = false
    }
  }, [])
  const names = repos.status === 'ok' || repos.status === 'stale' ? new Map(repos.data.map((r) => [r.repo_id, `${r.owner}/${r.repo}`])) : null
  return (
    <div className="sc-list">
      {rows.length === 0 ? (
        <Absent kind="zero" heading="No schedules yet" say="This tenant has no schedules: the list was read and is empty.">
          Recurring work in this tenant, such as an issue sweep, an index refresh or a nightly observer, is listed here. Each firing makes ordinary work: an issue run, a task or a workflow.
        </Absent>
      ) : (
        <ScheduleList rows={rows} names={names} go={go} />
      )}
      <h2 className="sc-h2">Built-in recurring work</h2>
      <BuiltInRows repos={repos} go={go} />
    </div>
  )
}

export function SchedulesScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const scheduleId = scheduleOf(view)
  if (scheduleId !== null) return <ScheduleDetail scheduleId={scheduleId} go={go} />
  return (
    <Screen
      title="Schedules"
      load={loadSchedules}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')} · tenant ${d.tenant_id}`}
    >
      {(d) => <SchedulesBody rows={d.schedules} go={go} />}
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
