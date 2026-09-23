import { useEffect, useMemo, useState } from 'react'
// `Em` and `Mark` live in AgentDetail.tsx, which is where `ABSENT_MARK` was
// written and which design-system.md §9.1 names as the source to promote from.
// One definition for the four screens of this group; a second copy of a mark
// whose whole job is to be recognisable is a contradiction in terms.
import { Em, Mark } from './AgentDetail'
import { loadTasks } from './api'
import { DispatchChip } from './Dispatch'
import { HelpCard } from './HelpCard'
import { Id, Screen } from './Shell'
import {
  CONCURRENCY_STATES,
  RESOURCE_UNITS,
  TERMINAL_STATES,
  elapsed,
  rollupState,
  stateGlyph,
  stateTone,
  whyAgent,
  type Task,
  type TaskPage,
} from './types'

type Tab = 'live' | 'waiting' | 'recent'

/**
 * THE THREE TABS, AND WHERE THEIR SENTENCES WENT.
 *
 * `hint` was a `title=` on each tab -- "Holding a pool slot, and costing
 * money" -- which is exactly the medium design-system.md §8.3 names as the one
 * that fails: no visible anchor, no keyboard route, invisible in a screenshot.
 * It is `aria-label` now, which a keyboard and a screen reader both reach, and
 * the tab's own word plus its count is what a sighted reader needs. Which
 * states cost nothing is `#help/capacity`.
 */
const TABS: { id: Tab; label: string; say: string }[] = [
  { id: 'live', label: 'Live', say: 'Live: holding a pool slot, and costing money' },
  { id: 'waiting', label: 'Waiting', say: 'Waiting: durable and free — READY or PARKED' },
  { id: 'recent', label: 'Recent', say: 'Recent: finished, one way or another' },
]

function tabOf(t: Task): Tab {
  if (CONCURRENCY_STATES.has(t.state)) return 'live'
  if (TERMINAL_STATES.has(t.state)) return 'recent'
  return 'waiting'
}

/**
 * Screen A -- Agents. One table, three tabs, no sub-pages.
 *
 * The tab counts come from the ROWS, never from /v1/stats, so the badge and
 * the table can never disagree with each other.
 */
export function AgentsScreen({ onOpen }: { onOpen: (taskId: string) => void }) {
  const [tab, setTab] = useState<Tab>('live')
  const [profile, setProfile] = useState<string>('')
  const [grouped, setGrouped] = useState(false)

  return (
    <Screen
      title="Agents"
      load={loadTasks}
      summary={(d) => {
        const live = d.tasks.filter((t) => tabOf(t) === 'live').length
        return (
          <>
            {d.tasks.length} loaded · {live} live
            {d.tenant_id && ` · ${d.tenant_id}`}
          </>
        )
      }}
      empty={{
        heading: 'No agents · real zero',
        // ONE SENTENCE, WHICH IS WHAT §6.9 ALLOWS AN EMPTY STATE. The second
        // sentence -- "nothing has been submitted under this tenant, or
        // everything has aged out of the page" -- was two guesses about a
        // cause this screen cannot see, and the link is where a reader finds
        // out which states a page holds.
        body: (
          <>
            The read succeeded and returned nothing. <HelpCard topic="tenant-scope" />
          </>
        ),
      }}
    >
      {(d) => (
        <AgentsBody
          onOpen={onOpen}
          page={d}
          tab={tab}
          setTab={setTab}
          profile={profile}
          setProfile={setProfile}
          grouped={grouped}
          setGrouped={setGrouped}
        />
      )}
    </Screen>
  )
}

function AgentsBody({
  onOpen,
  page,
  tab,
  setTab,
  profile,
  setProfile,
  grouped,
  setGrouped,
}: {
  onOpen: (taskId: string) => void
  page: TaskPage
  tab: Tab
  setTab: (t: Tab) => void
  profile: string
  setProfile: (p: string) => void
  grouped: boolean
  setGrouped: (g: boolean) => void
}) {
  // One clock for every ticking duration on the screen, so a hundred rows do
  // not each hold their own interval.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const counts = useMemo(() => {
    const c: Record<Tab, number> = { live: 0, waiting: 0, recent: 0 }
    for (const t of page.tasks) c[tabOf(t)]++
    return c
  }, [page.tasks])

  const profiles = useMemo(
    () => Array.from(new Set(page.tasks.map((t) => t.runner_profile))).sort(),
    [page.tasks],
  )

  const rows = useMemo(
    () =>
      page.tasks
        .filter((t) => tabOf(t) === tab)
        .filter((t) => profile === '' || t.runner_profile === profile)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)),
    [page.tasks, tab, profile],
  )

  // THE SCOPE OF EVERY FIGURE ABOVE, IN ONE QUALIFIER.
  //
  // Two sentences carried this -- "filters apply to the N loaded rows" above
  // the table and "More rows exist beyond this page. Counts and grouping above
  // describe only what is loaded." below it -- and they said the same thing
  // twice, about the same page, in two places a reader has to hold together.
  // It is one `.ctl-card-note`-shaped qualifier in the toolbar now, carrying
  // the figure that varies and the fact that there is more; the sentence is
  // its accessible name. A count that looks server-side but is not is the same
  // lie as an error rendered as an empty list, so the qualifier is never
  // conditional on there being a next page -- only its second half is.
  const scopeSay = page.next_page_token
    ? `Every count and filter on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform. More rows exist beyond it.`
    : `Every count and filter on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform.`

  return (
    <>
      <div className="ctl-toolbar">
        <div className="ctl-seg" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              aria-label={t.say}
              onClick={() => setTab(t.id)}
            >
              {t.label} <span className="badge">{counts[t.id]}</span>
            </button>
          ))}
        </div>

        <label className="ag-filter">
          <span className="ctl-eyebrow">profile</span>
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">all</option>
            {profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        {tab !== 'live' && (
          <label className="ag-filter check">
            <input
              type="checkbox"
              checked={grouped}
              onChange={(e) => setGrouped(e.target.checked)}
            />
            Group by workflow
          </label>
        )}

        <span className="is-end ag-scope" aria-label={scopeSay}>
          {page.tasks.length} loaded{page.next_page_token ? ' · more beyond' : ''}
        </span>
      </div>

      {rows.length === 0 ? (
        // A REAL ZERO, DRAWN AS ONE. The mark is the fact -- two words, always
        // rendered, legible with every card shut -- and the sentence that used
        // to stand here is its accessible name. Which states cost nothing is
        // `#help/capacity`, read from the set that owns them.
        <div className="ctl-empty">
          <h3>
            <Mark
              kind="zero"
              say={
                tab === 'live'
                  ? 'No agent is holding a pool slot right now. This is a real zero from a successful read, not a failed one.'
                  : tab === 'waiting'
                    ? 'Nothing is waiting. Waiting work costs nothing, so an empty tab here is normal.'
                    : 'Nothing has finished in the loaded page.'
              }
            />{' '}
            nothing in {tab}
            <HelpCard topic="capacity" />
          </h3>
        </div>
      ) : grouped && tab !== 'live' ? (
        <GroupedRows rows={rows} now={now} onOpen={onOpen} />
      ) : (
        <div className="rows">
          {rows.map((t) => (
            <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} />
          ))}
        </div>
      )}
    </>
  )
}

/**
 * "Agents grouped by workflow" -- the list half of the request.
 *
 * Client-side over the loaded page, and labelled as such: a header saying
 * "3 steps" when the workflow has nine and six fell off page one is the same
 * lie in a new place.
 */
function GroupedRows({
  rows,
  now,
  onOpen,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
}) {
  const groups = useMemo(() => {
    const m = new Map<string, Task[]>()
    for (const t of rows) {
      const key = t.workflow_id ?? ''
      const list = m.get(key)
      if (list) list.push(t)
      else m.set(key, [t])
    }
    // Standalone agents collect under one group, last.
    return Array.from(m.entries()).sort(([a], [b]) =>
      a === '' ? 1 : b === '' ? -1 : a.localeCompare(b),
    )
  }, [rows])

  return (
    <>
      {groups.map(([wf, tasks]) => {
        const roll = rollupState(tasks.map((t) => t.state))
        return (
          <section className="section group" key={wf || 'standalone'}>
            <h2>
              {/* B17: the same workflow id the Workflows screen prints, and
                  the same reason it is not uppercased here either. "No
                  workflow" is a sentence, not an id, so it is not wrapped. */}
              {wf === '' ? 'No workflow' : <Id>{wf}</Id>}
              <span className={`roll ${roll}`}>{roll}</span>
              {/* THE FIGURE IS THE FACT. "3 steps in this page" said `3` and
                  then re-said, in four more words, the thing the toolbar's
                  scope qualifier already says once for the whole screen. A
                  header saying "3 steps" when the workflow has nine and six
                  fell off page one is the lie this qualifier exists to
                  prevent, and the count plus the mark is the shape of it. */}
              <span
                className="ag-scope"
                aria-label={`${tasks.length} of this workflow's steps are in the loaded page. The workflow may have more; this grouping is client-side over what was loaded.`}
              >
                {tasks.length} here
              </span>
            </h2>
            <div className="rows">
              {tasks.map((t) => (
                <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} />
              ))}
            </div>
          </section>
        )
      })}
    </>
  )
}

function TaskRow({
  task,
  now,
  onOpen,
}: {
  task: Task
  now: number
  onOpen: (taskId: string) => void
}) {
  const holding = CONCURRENCY_STATES.has(task.state)
  const why = whyAgent(task)
  const el = elapsed(task, now)
  const units = RESOURCE_UNITS[task.resource_class]
  // Driven by the flag, not by an optimistic state flip. A cancel on a LEASED
  // or RUNNING task writes only cancel_requested -- the state does not change
  // until the worker or reconciler releases the lease, because releasing it
  // from the API would decrement a pool a live container still occupies.
  const cancelling = task.cancel_requested && !TERMINAL_STATES.has(task.state)

  return (
    <div
      className={`row clickable${holding ? ' holding' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(task.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen(task.id)
        }
      }}
    >
      {/* THE STATE DOT, AHEAD OF THE WORD. `.ctl-dot`'s seven silhouettes are
          the same vocabulary the chips use, so the shape is readable before
          the word is -- which is what makes forty rows scannable down the left
          edge rather than readable one at a time. The WORD IS STILL MANDATORY:
          colour and shape are the second and third signals, never the only
          one. */}
      <span className={`st ${stateTone(task.state)}`}>
        <i className={`ctl-dot is-${stateTone(task.state) === 'wait' ? 'warn' : stateTone(task.state)}`} aria-hidden />
        <span aria-hidden>{stateGlyph(task.state)}</span> {task.state}
      </span>

      <span className="agent">
        <b>{task.runner_profile}</b>
        {task.model && <span className="model">{task.model}</span>}
        <span className="id" title={task.id}>
          {task.id.slice(-8)}
        </span>
      </span>

      <span className="owner" title={task.submitted_by ?? undefined}>
        {task.submitted_by?.split('@')[0] ?? <Em />}
      </span>

      <span className="wf">
        {/* `.tag` uppercases, and a step id is the string the DAG is built
            from and the one a 422 names back. One rule for every identifier on
            every screen, and it is `<Id>`; see `.id` in styles.css. */}
        {task.step_id ? (
          <span className="tag" aria-label={`workflow ${task.workflow_id}`}>
            <Id>{task.step_id}</Id>
          </span>
        ) : (
          <span className="ctl-em">—</span>
        )}
      </span>

      <span className={`when${el.ticking ? ' ticking' : ''}`}>{el.text}</span>

      <span className="try" aria-label={`${task.attempt_count} of ${task.max_attempts} attempts used`}>
        {task.attempt_count}/{task.max_attempts}
      </span>

      <span className="class">
        {task.resource_class}
        {units !== undefined && <span className="units"> · {units}u</span>}
      </span>

      {/* The dispatch chip renders NOTHING for a plain `collect` task, which is
          most of them, and something for every task that will push or open a
          pull request. That asymmetry is the point: the rows worth spotting in
          a list of forty are the ones that are going to write to a repository. */}
      <span className="badges">
        <DispatchChip task={task} />
        {cancelling && <span className="tag full">cancelling…</span>}
      </span>

      {/* The reason this screen exists on a phone: someone is checking why
          their agent has not moved. It outranks every identifier and is never
          the thing that gets dropped. */}
      {why && <span className="why">{why}</span>}
    </div>
  )
}
