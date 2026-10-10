/**
 * AUTOMATE › SCHEDULES (docs/schedules.md §6.1-§6.2, lane S7; owner decision
 * 2026-10-08, SD1).
 *
 * The list is variant A, a table: the columns the owner listed fit at 1280px
 * and each row drops to a card of two lines on a phone. Under the tenant's
 * schedules sit the BUILT-IN ROWS (SD7, §8.3): each registration's index
 * cadence, read-only, linking to the repository's Settings, so every piece of
 * recurring work is on one page although the index keeps its own trigger.
 *
 * One schedule (`/schedules/<id>`) is ScheduleDetail.tsx; the create and edit
 * form (`/schedules/new`, `/schedules/<id>/edit`) is ScheduleEdit.tsx. Both
 * ride on this tab's query, as one run rides on Runs'.
 *
 * AN UNKNOWN IS A DASH WITH ITS REASON, NEVER 0 (§6.2). Spend today with an
 * unreported attempt is `$3.10 · 2 attempts unreported` beside a partial mark;
 * a schedule that never fired says so rather than "0 runs".
 */
import { useEffect, useState, type ReactNode } from 'react'
import { loadRepositories, loadSchedules, type Schedule, type ScheduleState, type SpendToday } from './api'
import { ButtonLink } from './components'
import { Dash } from './components/Chip'
import { addressToPath } from './paths'
import { Absent, Mark } from './primitives'
import type { RepoRecord } from './RepositoriesData'
import { ScheduleDetail } from './ScheduleDetail'
import { ScheduleEditScreen } from './ScheduleEdit'
import { Screen } from './Shell'
import { pluralise, timeAgo } from './types'
import { useNow } from './useNow'
import './styles/automate.css'

/** The tab's address, and one schedule's. */
export const SCHEDULES = 'automate/schedules'
export function scheduleAddress(id: string, extra: Record<string, string> = {}): string {
  return `${SCHEDULES}?${new URLSearchParams({ schedule: id, ...extra }).toString()}`
}

/** A link the router takes on a plain click, and the browser on any other. */
export function RoutedLink({ to, go, className, children }: { to: string; go: (to: string) => void; className?: string; children: ReactNode }) {
  return (
    <a
      className={className ?? 'ctl-link'}
      href={addressToPath(to)}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        go(to)
      }}
    >
      {children}
    </a>
  )
}

/** A time in the schedule's own zone: `Mon 09:00`, with the zone named once beside it. */
export function whenIn(iso: string | null | undefined, timezone: string): string | null {
  if (iso === null || iso === undefined) return null
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return null
  try {
    return d.toLocaleString('en-GB', { timeZone: timezone, weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  } catch {
    // A zone this browser does not know: the instant in UTC, said as UTC.
    return `${d.toISOString().slice(0, 16).replace('T', ' ')} UTC`
  }
}

export const STATE_WORDS: Readonly<Record<ScheduleState, string>> = {
  enabled: 'Enabled',
  paused: 'Paused',
  auto_paused: 'Auto-paused',
  disabled: 'Disabled by an admin',
}

/** A state this client does not know is printed as served, never mapped to one it does. */
export function stateWord(state: string): string {
  return (STATE_WORDS as Readonly<Record<string, string>>)[state] ?? state
}

/** Today's spend: the reported figure, and, when an attempt never reported, a partial mark saying so. */
export function SpendCell({ spend }: { spend: SpendToday | null | undefined }) {
  if (spend === null || spend === undefined || typeof spend.reported_usd !== 'number') {
    return <Dash why="not served: the API sent no spend for today" />
  }
  const usd = `$${spend.reported_usd.toFixed(2)}`
  if (spend.coverage !== 'partial') return <span className="au-num">{usd}</span>
  const n = spend.unreported_attempts
  const unreported = `${pluralise(n, 'attempt')} unreported`
  return (
    <span className="au-spend">
      <span className="au-num">{usd}</span> · {unreported}{' '}
      <Mark kind="partial" say={`${usd} is a floor: ${unreported}, whose cost is not in it`} />
    </span>
  )
}

/** The last firing: when and how it ended, or that there has never been one. */
export function LastRun({ s, now }: { s: Pick<Schedule, 'last_firing'>; now: number }) {
  const last = s.last_firing
  if (last === null || last === undefined) return <span className="au-dim">never fired</span>
  const at = last.ended_at ?? last.slot ?? null
  return (
    <span>
      {last.outcome ?? 'in progress'}
      {at !== null && <span className="au-dim"> · {timeAgo(at, now)}</span>}
    </span>
  )
}

/** The scope in words: `all repositories`, or the repositories by name where the list knows them. */
export function scopeWords(s: Pick<Schedule, 'scope'>, repos: ReadonlyMap<string, string>): string {
  if (s.scope.mode === 'all') return 'all repositories'
  if (s.scope.mode === 'platform') return 'the platform'
  const ids = s.scope.repo_ids ?? []
  if (ids.length === 0) return 'no repository'
  return ids.map((id) => repos.get(id) ?? id).join(', ')
}

/** Which page of this tab the query names. */
function pageOf(view: string | null): { id: string | null; tab: string | null; page: string | null } {
  const q = new URLSearchParams(view ?? '')
  const id = q.get('schedule')
  return { id: id === '' ? null : id, tab: q.get('tab'), page: q.get('page') }
}

export function SchedulesScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const at = pageOf(view)
  if (at.page === 'new') return <ScheduleEditScreen id={null} go={go} />
  if (at.id !== null && at.page === 'edit') return <ScheduleEditScreen id={at.id} go={go} />
  if (at.id !== null) return <ScheduleDetail id={at.id} tab={at.tab} go={go} />
  return (
    <Screen
      title="Schedules"
      // An empty list is still a page: the built-in rows and the way to create one.
      load={async () => {
        const r = await loadSchedules()
        return r.status === 'empty' ? { status: 'ok' as const, fetchedAt: r.fetchedAt, data: { schedules: [], tenant_id: '' } } : r
      }}
      summary={(d) => `${pluralise(d.schedules.length, 'schedule')}, by name`}
    >
      {(d) => <ScheduleList schedules={d.schedules} go={go} />}
    </Screen>
  )
}

/** The registrations, for names and the built-in rows; a failure is said, never drawn as none. */
function useRepositories(): { rows: RepoRecord[] | null; error: string | null } {
  const [state, setState] = useState<{ rows: RepoRecord[] | null; error: string | null }>({ rows: null, error: null })
  useEffect(() => {
    let live = true
    void loadRepositories().then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') setState({ rows: r.data, error: null })
      else if (r.status === 'empty') setState({ rows: [], error: null })
      else if (r.status === 'error') setState({ rows: null, error: r.error.message })
    })
    return () => {
      live = false
    }
  }, [])
  return state
}

function ScheduleList({ schedules, go }: { schedules: Schedule[]; go: (to: string) => void }) {
  const now = useNow()
  const repos = useRepositories()
  const [repo, setRepo] = useState('')
  const names = new Map((repos.rows ?? []).map((r) => [r.repo_id, `${r.owner}/${r.repo}`] as const))
  const shown = schedules.filter((s) => repo === '' || s.scope.mode === 'all' || (s.scope.repo_ids ?? []).includes(repo))
  const builtIn = (repos.rows ?? []).filter((r) => repo === '' || r.repo_id === repo)
  return (
    <div className="au-list">
      <div className="au-bar">
        <label className="au-field">
          <span>Repository</span>
          <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Repository">
            <option value="">All repositories</option>
            {(repos.rows ?? []).map((r) => (
              <option key={r.repo_id} value={r.repo_id}>
                {r.owner}/{r.repo}
              </option>
            ))}
          </select>
        </label>
        <ButtonLink
          kind="primary"
          size="sm"
          href={addressToPath(`${SCHEDULES}?page=new`)}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
            e.preventDefault()
            go(`${SCHEDULES}?page=new`)
          }}
        >
          New schedule
        </ButtonLink>
      </div>
      {schedules.length === 0 ? (
        <Absent kind="zero" heading="No schedules yet" say="This tenant has no schedules: a real zero, read from the API">
          A schedule runs a type of job on a cron, as you, behind the gate you set. New schedule creates one.
        </Absent>
      ) : (
        <div className="au-table-wrap">
          <table className="au-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Repository</th>
                <th scope="col">When</th>
                <th scope="col">Next run</th>
                <th scope="col">Last run</th>
                <th scope="col">Spend today</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <tr key={s.schedule_id} className="au-row" data-schedule={s.schedule_id}>
                  <td data-label="Name" className="au-name">
                    <RoutedLink to={scheduleAddress(s.schedule_id)} go={go}>
                      {s.name}
                    </RoutedLink>
                    {s.pending_approvals > 0 && (
                      <span className="au-pend" title={`${pluralise(s.pending_approvals, 'firing')} waiting for approval`}>
                        {s.pending_approvals} waiting
                      </span>
                    )}
                  </td>
                  <td data-label="Type" className="mono">{s.type}</td>
                  <td data-label="Repository">{scopeWords(s, names)}</td>
                  <td data-label="When">
                    {s.words ?? <Dash why={`the cron ${s.cron} did not parse`} />} <span className="au-dim">{s.timezone}</span>
                  </td>
                  <td data-label="Next run">
                    {s.state !== 'enabled' ? (
                      <Dash why={`not scheduled: the schedule is ${stateWord(s.state).toLowerCase()}`} />
                    ) : (
                      whenIn(s.next_run_at, s.timezone) ?? <Dash why="not served: the schedule carries no next run" />
                    )}
                  </td>
                  <td data-label="Last run">
                    <LastRun s={s} now={now} />
                  </td>
                  <td data-label="Spend today">
                    <SpendCell spend={s.spend_today} />
                  </td>
                  <td data-label="State" className={`au-state is-${s.state}`}>
                    {stateWord(s.state)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.length === 0 && <p className="au-dim">No schedule of the {schedules.length} reaches that repository.</p>}
        </div>
      )}
      <section className="au-builtin" aria-labelledby="au-builtin-h">
        <h2 id="au-builtin-h">Built in</h2>
        <p className="au-dim">
          Each repository re-indexes on its own trigger, set on its Settings tab (SD7). Shown here so all recurring work is in one place; they are not schedules and are not edited here.
        </p>
        {repos.error !== null ? (
          <Absent kind="failed" heading="The repositories were not read" say={`Not read: ${repos.error}`}>
            {repos.error}
          </Absent>
        ) : repos.rows === null ? (
          <p className="au-dim">Reading the repositories…</p>
        ) : builtIn.length === 0 ? (
          <Absent kind="zero" heading="No repository is registered" say="No registration, so no built-in index row" />
        ) : (
          <ul className="au-builtin-rows">
            {builtIn.map((r) => (
              <li key={r.repo_id} className="au-builtin-row">
                <span className="au-tag">built in</span>
                <span>
                  Index · <span className="mono">{r.owner}/{r.repo}</span> · {indexWords(r)}
                </span>
                <RoutedLink to={`work/repositories?${new URLSearchParams({ repo: r.repo_id, tab: 'settings' }).toString()}`} go={go}>
                  Settings
                </RoutedLink>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/**
 * The index's cadence in words, as §8.3 writes it: "on change, at most every
 * 30 min, and every 24 h". An unserved part is said to be unknown, never
 * dropped, so an unknown trigger cannot read as "off".
 */
export function indexWords(r: Pick<RepoRecord, 'index'>): string {
  const ix = r.index
  if (ix.paused === true) return 'paused'
  const parts: string[] = []
  if (ix.on_change === null) parts.push('change trigger unknown')
  else if (ix.on_change !== 'off') {
    const gap = ix.min_change_interval_minutes
    parts.push(`on change${ix.on_change === 'webhook' ? ' (webhook)' : ''}${gap === null ? '' : `, at most every ${gap} min`}`)
  }
  if (ix.interval_hours === null) parts.push('interval unknown')
  else if (ix.interval_hours !== 'off') parts.push(`every ${ix.interval_hours} h`)
  return parts.length === 0 ? 'off' : parts.join(', and ')
}
