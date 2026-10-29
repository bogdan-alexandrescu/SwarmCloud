import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { approvePlan, editPlan, loadRun, loadRuns, loadSchedules, rejectPlan } from './api'
import type { ApiError, Result } from './fetch'
import { InlineText as RnText, runAddress } from './IssueSubmit'
import { MarkGlyph, type MarkHue, type MarkName } from './marks'
import { Banner, Button, Card, Chip, CIcon, CodeBlock, Segmented, Tabs, WarnMark } from './components'
import { Dash } from './components/Chip'
import { FailedPanel, Screen, timeAgo } from './Shell'
import { workflowPullRequest, type MergeCard, type WorkflowPullRequest } from './stepviews'
import { pluralise } from './types'
import {
  CostFact, PlanStepLive, planStepRow, previousAt, ProgressTime, progressEntries, redReading, shortSha, StepProgress, stepRows, StepsCard, useRunWorkflows,
  type ProgressEntry, type RunWorkflows, type StepRow,
} from './RunSteps'
import type { IssueRun, IssueRunState, IssueRunSummary, IssueRunSummaryPage, OpenWork, PlanOverlap, PlanStepDoc, RunCheckCounts, RunPlan } from './types'
import { useNow } from './useNow'
import { proseWords, stateWord } from './words'
import { RunContextCard, SelectedTestsGate, useRunIndex, type RunIndexRead } from './RunIndex'
import { addressToPath } from './paths'
import { Absent } from './primitives'
import { RunChangesTab, changesAtOf, withChangesAt } from './WorkflowChanges'
import { Markdown } from './ArtifactViewer'
import './styles/intake.css'
import './styles/runs.css'

/**
 * WORK › RUNS (intake-tenants.html 1A, picked 2026-10-02): `/runs`, this
 * tenant's issue runs newest first, and `/runs/<id>`, one run.
 *
 * EVERY READ ADVANCES THE RUN (routes/runs.py): the API moves a run from
 * PLANNING to PLANNED, and from RUNNING to its end, when a read finds its task
 * or workflow has moved. So a run that is not finished is re-read on a
 * cadence; a finished one is read once.
 *
 * THE DIGEST IS THE APPROVAL (owner decision D3). Approve sends the digest of
 * the plan ON THIS SCREEN, and the API refuses it with 409 `plan_changed` when
 * the plan has been edited since -- this page then re-reads the run and says
 * so, rather than approving whatever the plan had become. Edit and Reject
 * carry the shown digest for the same reason.
 *
 * THE OVERLAPS COME FIRST (#454's acceptance test is that the plan names
 * them): each is a link to the issue or pull request the planner found in
 * flight, with its kind and the planner's note, above the steps. A plan
 * written before the planner read open work does not say, and the page says
 * that -- never "none", which would be a finding nobody made.
 *
 * EVERYTHING READ FROM GITHUB OR WRITTEN BY THE PLANNER IS TEXT. An overlap's
 * ref becomes a link only when it is `owner/repo#N` (issueruns'
 * OVERLAP_REF_PATTERN); a pull request's URL only when it is on github.com;
 * the failing-check excerpt is drawn as preformatted text, never as HTML.
 *
 * WHAT THE RUN HAS NOT GOT YET IS SAID, NOT HIDDEN: a pull request not yet
 * opened, a comment not yet posted, a keyword the review has not decided.
 * Cost is summed from the step tasks that report one, with how many do; none
 * reporting is a dash with that reason, never $0. A workflow not
 * yet created is "none yet", because the run does serve `workflow_id`, and
 * null is a fact.
 *
 * THE PAGE LEADS WITH THE ISSUE (lane U9, owner 2026-10-03). Its title is the
 * issue's title as the run read it at submission (`issue_read`), its meta
 * `owner/repo#N · run_… · created by …`; the plan is drawn from its schema
 * -- a short lead, numbered steps with their prompts folded, the raw plan
 * behind a disclosure -- rather than as one long paragraph.
 *
 * THE PAGE FOLLOWS THE RUN (lane U14, owner 2026-10-04). While the run has a
 * workflow, a Steps card under the status line lists every step with its
 * state and a link to its agent (RunSteps.tsx); Progress interleaves the
 * steps' changes; cost is the step tasks' recorded cost with its coverage.
 * From CHECKING the pull request card leads, and once the plan is approved
 * the overlaps and the plan fold to one line each.
 *
 * Classes are `rn-` so a later pass can swap them for lane U0's components.
 */

/** A run that has not finished is re-read on this cadence: each read advances it. */
export const RUN_POLL_MS = 15_000

/** `issueruns.AUTO_APPROVER`: who `approved_by` names on an automatic approval. */
const AUTO_APPROVER = 'auto-approval'

/** `issueruns.OVERLAP_REF_PATTERN`: the only overlap ref that is made a link. */
const GITHUB_REF = /^([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9._-]{1,100})#([1-9][0-9]{0,9})$/

/**
 * Where an overlap points on GitHub, or null when its ref is not `owner/repo#N`.
 * The planner wrote it, so it is data: anything else is drawn as text.
 */
export function overlapUrl(o: PlanOverlap): string | null {
  const m = GITHUB_REF.exec(o.ref)
  if (m === null) return null
  return `https://github.com/${m[1]}/${m[2]}/${o.kind === 'pull_request' ? 'pull' : 'issues'}/${m[3]}`
}

/** A link the API served from GitHub, kept only when it is on github.com over https. */
function githubLink(url: string | null | undefined): string | null {
  return typeof url === 'string' && url.startsWith('https://github.com/') ? url : null
}

/**
 * A check's own link (#503): a check run's html/details url or a status's
 * target url. Wider than `githubLink` on purpose -- a CI that reports through
 * the statuses API links its own site -- but https only, so a served string
 * can never become a `javascript:` or plain-http href.
 */
function checkLink(url: string | null | undefined): string | null {
  return typeof url === 'string' && url.startsWith('https://') ? url : null
}

/** An instant as absolute UTC, for a title beside a relative age. */
function utcTitle(iso: string): string {
  const t = new Date(iso)
  return Number.isFinite(t.getTime()) ? `${t.toISOString().slice(0, 19).replace('T', ' ')} UTC` : iso
}

/** `pull_request.check_counts` as the card says it: the non-zero buckets, in GitHub's order. */
export function checkCountsText(counts: RunCheckCounts): string {
  const parts = (['passed', 'failed', 'pending', 'skipped'] as const)
    .filter((k) => counts[k] > 0)
    .map((k) => `${counts[k]} ${k}`)
  return parts.length === 0 ? 'no checks reported' : parts.join(' · ')
}

/** A comment on the run's issue, by the id the write-back recorded. */
function commentUrl(run: IssueRun, id: number | null | undefined): string | null {
  const issue = githubLink(run.issue.url)
  return issue === null || typeof id !== 'number' ? null : `${issue}#issuecomment-${id}`
}

/** The brand state marks (marks.tsx) for a run's states. */
const RUN_MARK: Readonly<Record<IssueRunState, { mark: MarkName; hue: MarkHue }>> = {
  // A planner task is working: it can hold capacity, as any task.
  PLANNING: { mark: 'running', hue: 'live' },
  // Waiting for a person, holding nothing (invariant 1): a park.
  PLANNED: { mark: 'parked', hue: 'park' },
  APPROVED: { mark: 'starting', hue: 'live' },
  RUNNING: { mark: 'running', hue: 'live' },
  // Waiting on CI, holding nothing (invariant 1): a park, like PLANNED.
  CHECKING: { mark: 'parked', hue: 'park' },
  // One continuation is fixing CI: a task working, as RUNNING.
  FIXING: { mark: 'running', hue: 'live' },
  DONE: { mark: 'succeeded', hue: 'neu' },
  FAILED: { mark: 'failed', hue: 'bad' },
  REJECTED: { mark: 'cancelled', hue: 'neu' },
  CANCELLED: { mark: 'cancelled', hue: 'neu' },
  // The planner's answer that the issue is not ready: ended, holding nothing.
  NOT_READY: { mark: 'parked', hue: 'neu' },
}

/** `issuesweep.SWEEP_CREATOR`: what a run the issue sweeper started records as its creator. */
export const SWEEP_CREATOR = 'issue-sweep'

/**
 * A SCHEDULE IS NAMED, NEVER ITS BARE ID (QA1-FIX-24, chrome-C12, part of
 * #2596). A run a schedule fired records `schedule:<schedule_id>` as its
 * creator (schedulefire.py), and the list's By column, the run's head and its
 * Progress rows printed that -- `schedule:sch_54e9828e6772`, cut in the list
 * -- where the reader knows the schedule as "Failure review". The tenant's
 * schedules are read once, only when a schedule appears, and the id is kept
 * in the title; a name that could not be read says so beside the id.
 */
const SCHEDULE_BY = /^schedule:(.+)$/

/** The schedule id a `created_by` / `by` names, or null for a person or a service. */
export function scheduleOf(by: string | null | undefined): string | null {
  const m = SCHEDULE_BY.exec(by ?? '')
  return m === null ? null : m[1]!
}

/** The tenant's schedules' names by id; null until read (or when no schedule is named). */
export type ScheduleNames = Readonly<Record<string, string>> | null

/** `by` in words: a schedule by its name, anyone else as recorded. */
export function byWord(by: string, names: ScheduleNames = null): string {
  const id = scheduleOf(by)
  if (id === null) return by
  const name = names?.[id]
  return name ? `schedule ${name}` : `schedule ${id} · name not read`
}

/** Reads the schedules' names once `wanted` turns true; a failed read leaves the ids, said so. */
export function useScheduleNames(wanted: boolean): ScheduleNames {
  const [names, setNames] = useState<ScheduleNames>(null)
  useEffect(() => {
    if (!wanted || names !== null) return
    let live = true
    void loadSchedules().then((r) => {
      if (!live || (r.status !== 'ok' && r.status !== 'stale')) return
      setNames(Object.fromEntries(r.data.schedules.map((x) => [x.schedule_id, x.name])))
    })
    return () => { live = false }
  }, [wanted, names])
  return names
}

/** Who a run is by, as the list and the run page say it. */
export function runCreatorWord(run: IssueRunSummary, names: ScheduleNames = null): string | null {
  if (run.created_by === SWEEP_CREATOR) {
    return run.on_behalf_of ? `issue sweep, as ${run.on_behalf_of}` : 'issue sweep'
  }
  return run.created_by ? byWord(run.created_by, names) : null
}

/** A NOT_READY run's reason, or why an auto approval waits; null otherwise. */
export function runWhyLine(run: IssueRunSummary): string | null {
  if (run.state === 'NOT_READY') {
    const reason = run.not_ready?.reason?.trim()
    return reason ? `Not ready: ${reason}` : 'Not ready: the planner gave no reason.'
  }
  if (run.state === 'PLANNED' && run.hold) return `Waiting: ${run.hold}`
  return null
}

/** The states a run reaches only through an approved plan. */
const PAST_APPROVAL: ReadonlySet<IssueRunState> = new Set(['APPROVED', 'RUNNING', 'CHECKING', 'FIXING', 'DONE'])

/**
 * THE PLAN APPROVAL CELL SAYS PENDING OR GIVEN (visual QA run 6, work-a-010,
 * part of #1680). It printed `plan_approval` as served, so a Planned run with
 * no workflow and a Running one built from its approved plan both read
 * "required": the column could not tell a plan waiting for a person from one
 * already approved. A workflow, an approval time or a state past Planned is
 * an approval given; an automatic approval needs none, and says so in the
 * cell's title.
 *
 * EVERY WORD FITS THE COLUMN ON ONE LINE (QAFIX6-WORKPAGES-2, part of #1680).
 * The column is 86px (intake.css), 74px of content, and the row's cells are
 * drawn at --t-meta (13px) and never wrap. 'awaiting approval' (~124px at
 * the tests' conservative 0.56em) ran over Workflow, so it was drawn small
 * and wrapped; the words are now ones that fit at the body size. 'not
 * required' (~87px) and 'none needed' (~80px) do not, so an automatic
 * approval is a dash, and 'awaiting a plan' (~109px) is 'no plan'.
 * qafix6.workpages.test.tsx measures every word against the column.
 *
 * AN AUTOMATIC APPROVAL IS A WORD, NOT A DASH (QA1-FIX-24, chrome-C13, part
 * of #2596): the dash read as unknown on every row, runs the schedule's
 * automatic gate had approved among them. An approved run says 'approved'
 * whoever gave it (the cell's title says automatically); one still waiting
 * for its automatic gate says 'automatic' (~66px, inside the 74px).
 */
export function planApprovalWord(run: IssueRunSummary): string {
  if (run.state === 'REJECTED') return 'rejected'
  if (run.workflow_id !== null || run.approved_at !== null || PAST_APPROVAL.has(run.state)) return 'approved'
  if (run.plan_approval === 'auto' && run.state !== 'PLANNING') return 'automatic'
  if (run.state === 'PLANNED') return 'awaiting'
  if (run.state === 'PLANNING') return 'no plan'
  return 'not given'
}

/** A run's state in words: sentence case, never the API's capitals (PICKS.md, no all-caps). */
export function runStateWord(state: IssueRunState): string {
  return (state.charAt(0) + state.slice(1).toLowerCase()).replace(/_/g, ' ')
}

/**
 * A run's state as the canonical state pill (components.html A, `c-pill`):
 * the brand mark and the state in sentence case. It was the mark beside the
 * API's word in capitals -- "RUNNING", "DONE" -- against the no-all-caps rule
 * (browser QA D11, 2026-10-04). The API's spelling stays its title.
 */
export function RunStateMark({ state }: { state: IssueRunState }) {
  const { mark, hue } = RUN_MARK[state] ?? { mark: 'queued', hue: 'neu' }
  return (
    <span className={`c-pill is-${hue}`} data-mark={mark} data-hue={hue} title={state}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={mark} />
      </svg>
      {runStateWord(state)}
    </span>
  )
}

/** `sha256:9f2c41…e7`: enough to compare by eye; the whole digest is its title. */
export function shortDigest(digest: string): string {
  return digest.length > 18 ? `${digest.slice(0, 13)}…${digest.slice(-2)}` : digest
}

/** The run a Runs address names (`run=<id>`), or null for the list. */
function runOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('run')
  return id === null || id === '' ? null : id
}

/** One run's tabs: its page, and its Changes (`/runs/<id>/changes`, paths.ts `RUN_PANES`). */
type RunTab = 'run' | 'changes'

/** The tab a Runs address names (`tab=changes`); anything else is the run's page. */
function runTabOf(view: string | null): RunTab {
  return new URLSearchParams(view ?? '').get('tab') === 'changes' ? 'changes' : 'run'
}

/**
 * THE RUN'S TABS (docs/design/diff-viewer.md §2 variant 2): the run's page,
 * and Changes beside it, each its own address so a link can open either.
 */
function RunTabs({ runId, tab, go }: { runId: string; tab: RunTab; go: (to: string) => void }) {
  const base = runAddress(runId)
  const to: Readonly<Record<RunTab, string>> = { run: base, changes: `${base}&tab=changes` }
  const href: Readonly<Record<RunTab, string>> = { run: addressToPath(to.run), changes: addressToPath(to.changes) }
  return (
    <Tabs
      className="rn-tabs"
      label="Views of this run"
      current={tab}
      tabs={[
        { key: 'run', label: 'Run', href: href.run },
        { key: 'changes', label: 'Changes', href: href.changes },
      ]}
      onGo={(h) => go(h === href.changes ? to.changes : to.run)}
    />
  )
}

/** Tells the page its title on the Changes tab too, by the rule `RunPage` uses. */
function RunHeading({ run, onHeading }: { run: IssueRun; onHeading: (title: string) => void }) {
  const title = run.issue_read?.title || run.issue.ref
  // On the title alone: `onHeading` is a new function on every render of the screen.
  useEffect(() => onHeading(title), [title])
  return null
}

/**
 * THE META CHIP OPENS ON A TAP (owner QA F, 2026-10-04): on a phone the chip
 * `owner/repo#N · run_… · created by …` was cut to its first words, and a
 * title is no help where there is no hover. A tap draws it whole, wrapped; a
 * second folds it. Its text is its children, so the page head's `textOf`
 * still gives the chip its title.
 *
 * EACH FACT IS ITS OWN PIECE (visual QA V209, 2026-10-16): cut as one line,
 * the chip at 400px was 368 of 470px and showed the reference alone, so the
 * run id and its creator were drawn nowhere. The facts wrap between them now,
 * each keeping its ` ·` (never a separator opening a line), and only a fact
 * wider than the column is cut -- whole once the chip is opened.
 */
function RunMeta({ children }: { children: string }) {
  const [open, setOpen] = useState(false)
  const facts = children.split(' · ')
  return (
    <button type="button" className={open ? 'rn-meta is-open' : 'rn-meta'} aria-expanded={open} title={children}
      onClick={() => setOpen((o) => !o)}>
      {facts.map((f, i) => (
        <span key={i}>
          {i > 0 && ' '}
          <span className="rn-meta-i">{i < facts.length - 1 ? `${f} ·` : f}</span>
        </span>
      ))}
    </button>
  )
}

/**
 * A value cut ONLY at a safe point (owner QA A and C, 2026-10-04): after a
 * `/`, `#`, `_` or `::` -- the joints of a reference, a path or a test id --
 * and at a space. Each joint is followed by a `<wbr>`, and the value's
 * `overflow-wrap` stays normal, so a line never breaks mid-identifier
 * ('…_is_rec / orded_on…' was the measured break). `marks` narrows the joints
 * (a test id's list breaks at `/`, `::` and `_` only).
 */
export function breakAt(text: string, marks: RegExp = /::|[/#_]/g): ReactNode[] {
  const out: ReactNode[] = []
  let at = 0
  for (const m of text.matchAll(new RegExp(marks.source, 'g'))) {
    const end = (m.index ?? 0) + m[0].length
    if (end >= text.length || end === at) continue
    out.push(text.slice(at, end), <wbr key={end} />)
    at = end
  }
  out.push(text.slice(at))
  return out.filter((p) => p !== '')
}

/** A link inside the app: an href for a new tab, `go` for a plain click. */
function InApp({ to, href, go, children, cut = false, words = false }: {
  to: string; href: string; go: (to: string) => void; children: string
  /** A single-token id: one line, cut with an ellipsis (`.rn-id`), whole in its title. */
  cut?: boolean
  /** Words, not an id (`planner agent`, QA1 runs-submit-27): set in the page's face, not mono. */
  words?: boolean
}) {
  // Its whole text is its title: an id cut to its card says what it was (D3).
  return (
    <a href={href} className={cut ? 'mono rn-id' : words ? undefined : 'mono'} title={children} onClick={(e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      go(to)
    }}>{children}</a>
  )
}

export function RunsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const runId = runOf(view)
  const tab = runTabOf(view)
  const [reloads, setReloads] = useState(0)
  /** Why the run was re-read, when a refusal made this page do it. */
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => setNotice(null), [runId])
  /** The issue's title, once the run is read: the page's title (item 4). */
  const [heading, setHeading] = useState<{ run: string; title: string } | null>(null)
  // Set by the list or the run page once a schedule is among the creators shown.
  const [wantNames, setWantNames] = useState(false)
  const names = useScheduleNames(wantNames)
  const want = () => setWantNames(true)

  if (runId !== null) {
    // A local, not an inline expression: the nav-heading test reads the
    // literal below as the tab's heading and skips a per-run title. Until the
    // run is read, the id is all this page knows to call it.
    const pageTitle = heading !== null && heading.run === runId ? heading.title : runId
    const reread = (why: string) => {
      setNotice(why)
      setReloads((n) => n + 1)
    }
    // A heading with no space is a reference or an id: one line, cut, never
    // broken at the owner's hyphen (browser QA D11). A title wraps as prose.
    return (
      <div className={/\s/.test(pageTitle) ? 'rn-run' : 'rn-run is-ref'}>
        <a className="rn-back" href="/runs" onClick={(e) => {
          if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
          e.preventDefault()
          go('work/runs')
        }}>‹ All runs</a>
        {notice !== null && (
          <div className="rn-notice" role="status">
            <WarnMark />
            <span>{notice}</span>
          </div>
        )}
        <Screen
          key={`run:${runId}:${reloads}`}
          title={pageTitle}
          load={() => loadRun(runId)}
          pollMs={(d) => (d === null || d.run.terminal ? null : RUN_POLL_MS)}
          summary={(d) => <RunMeta>{`${d.run.issue.ref} · ${d.run.id} · created by ${d.run.created_by ? byWord(d.run.created_by, names) : '—'}`}</RunMeta>}
        >
          {(d) => {
            const onHeading = (title: string) => setHeading((was) => (was?.run === runId && was.title === title ? was : { run: runId, title }))
            return (
              <>
                <RunTabs runId={runId} tab={tab} go={go} />
                {tab === 'changes' ? (
                  <>
                    <RunHeading run={d.run} onHeading={onHeading} />
                    {d.run.workflow_id === null ? (
                      // THE RUN'S OWN WORDS FOR NO WORKFLOW (live QA RT-15): the
                      // shared empty state printed its legend word, `real zero
                      // no workflow yet`, and "yet" of a run that has ended.
                      <section className="chg-tab chg-run rn-nochanges" aria-label="Changes in this run">
                        <p className="sb-note">
                          {d.run.terminal
                            ? `No changes: the run ended (${stateWord(d.run.state)}) before a workflow was created, so no step ran.`
                            : "No changes yet: the run's workflow is created when its plan is approved, and until then no step has run."}
                        </p>
                      </section>
                    ) : <RunChangesTab
                      run={d.run}
                      at={changesAtOf(view)}
                      onAt={(at) => go(`${runAddress(runId)}&${withChangesAt('tab=changes', at)}`)}
                    />}
                  </>
                ) : (
                  <RunPage run={d.run} reread={reread} go={go} onHeading={onHeading} names={names} want={want} />
                )}
              </>
            )
          }}
        </Screen>
      </div>
    )
  }

  return (
    <Screen
      title="Runs"
      load={() => loadRuns()}
      // THE COUNT IS THE LIST'S (live QA RT-11, 2026-10-11): the head said
      // "50 shown · older runs exist" from the first page after "Show older
      // runs" had read the last 3. The list says how many it holds, under its
      // chips, and whether more exist; the head says only the order.
      summary={() => 'newest first'}
      pollMs={RUNS_LIST_POLL_MS}
      // THE WAY TO START ONE IS A LINK (visual QA V210, 2026-10-16): an empty
      // first page with no older runs is this panel, not the list's, and its
      // "Submit › From a GitHub issue" was plain text where the list's and
      // Workflows' are links.
      empty={{
        heading: 'No runs yet',
        body: <>This tenant has not planned an issue. <SubmitIssueLink go={go} /> starts one.</>,
      }}
    >
      {(d) => <RunList first={d} go={go} view={view} names={names} want={want} />}
    </Screen>
  )
}

/** The way to start a run: Submit's issue form, in place on a plain click. */
function SubmitIssueLink({ go }: { go: (to: string) => void }) {
  return (
    <a className="ctl-link rn-start" href={addressToPath('work/new-issue')} onClick={(e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      go('work/new-issue')
    }}>Submit › From a GitHub issue</a>
  )
}

// ---------------------------------------------------------------------------
// the list
// ---------------------------------------------------------------------------

/**
 * HOW OFTEN THE LIST RE-READS ITS FIRST PAGE (live QA RT-11, 2026-10-11): it
 * never refreshed by itself ("read 3 min ago") while a run page polled every
 * 15 s. A list is read for which runs exist and where each stands; a minute
 * is the Timeline's cadence for the same kind of reading.
 */
export const RUNS_LIST_POLL_MS = 60_000

/**
 * A ROW WITH NO ISSUE TITLE SAYS WHY (QA2-FIX-20, runs2-03). The Issue cell
 * leads with the title the run read at submission (`issue_read`); without one
 * the row was a bare `#512` beside a bare run id, nothing naming the work. The
 * reason is the read's own error where it failed, else that the run kept none.
 */
function NoTitle({ run }: { run: Pick<IssueRun, 'issue_read_error'> }) {
  const failed = run.issue_read_error ?? null
  const why = failed !== null
    ? `The issue could not be read when the run was created: ${failed.message}`
    : 'This run kept no reading of its issue: it was created before runs kept the title they read.'
  return (
    <i className="ctl-em rn-cut rn-issue-none" data-why={failed !== null ? 'failed' : 'not-kept'} title={why}>
      {failed !== null ? 'title not read · the read failed' : 'title not read · not kept on this run'}
    </i>
  )
}

/**
 * The Runs list's state chips (QA G2-18). `not ready` and `rejected` (live QA
 * RT-10): 11 of 50 runs were reachable only under `all`, and a not-ready run
 * is the one that waits on an owner's decision.
 */
/**
 * THE STATES IN THE WORKFLOWS FILTER'S ORDER AND CASE (visual QA QA-084,
 * 2026-10-20): Runs read 'all 2 · active 2 · done 0 …' beside Workflows'
 * 'Running 2 · Finished 0 · Failed 1 · All 3' on the next Work page. The
 * chips are capitalised (`RUN_FILTER_LABEL`) with All last; the keys stay
 * lowercase, so `?state=failed` addresses are unchanged.
 */
export const RUN_FILTERS = ['active', 'done', 'failed', 'not ready', 'rejected', 'all'] as const
export type RunFilter = (typeof RUN_FILTERS)[number]

const RUN_FILTER_LABEL: Readonly<Record<RunFilter, string>> = {
  active: 'Active',
  done: 'Done',
  failed: 'Failed',
  'not ready': 'Not ready',
  rejected: 'Rejected',
  all: 'All',
}

const RUN_FILTER_TEST: Readonly<Record<RunFilter, (r: IssueRunSummary) => boolean>> = {
  all: () => true,
  active: (r) => !r.terminal,
  done: (r) => r.state === 'DONE',
  failed: (r) => r.state === 'FAILED',
  'not ready': (r) => r.state === 'NOT_READY',
  rejected: (r) => r.state === 'REJECTED',
}

const RUN_FILTER_TITLE: Readonly<Record<RunFilter, string>> = {
  all: 'Every run read, rejected and cancelled ones included',
  active: 'Not finished: planning, waiting for approval, running, or in CI',
  done: 'Ended DONE',
  failed: 'Ended FAILED. A rejected or cancelled run is under All.',
  'not ready': 'The planner found the issue not ready, and said what it needs decided',
  rejected: 'Its plan was rejected',
}

/**
 * Whether a run matches the search: its issue ref, its issue's title, its id
 * or its workflows' ids, any case. The workflow id (live QA RT-12): a row
 * shows it, and searching for it found nothing.
 */
export function runMatches(r: IssueRunSummary, text: string): boolean {
  const q = text.trim().toLowerCase()
  if (q === '') return true
  return [r.issue.ref, r.issue_read?.title ?? '', r.id, r.workflow_id ?? '', ...(r.ci_fix_workflows ?? [])]
    .some((v) => typeof v === 'string' && v.toLowerCase().includes(q))
}

/**
 * THE LIST'S FILTER AND SEARCH, AS ITS ADDRESS (live QA RT-6, 2026-10-11):
 * choose `failed`, type `merge`, open a run, press Back, and the list was
 * `all` with an empty search. `?state=` (a chip, `all` omitted) and `?q=`
 * (the search) are read on mount and written in place, so Back and a reload
 * restore them, as the Workflows list's `?state=` does.
 */
export function runListQuery(view: string | null): { which: RunFilter; text: string } {
  const p = new URLSearchParams(view ?? '')
  const state = (p.get('state') ?? '').replace(/[-_]/g, ' ').toLowerCase()
  const which = (RUN_FILTERS as readonly string[]).includes(state) ? (state as RunFilter) : 'all'
  return { which, text: p.get('q') ?? '' }
}

export function runListQueryString(which: RunFilter, text: string): string {
  const p = new URLSearchParams()
  if (which !== 'all') p.set('state', which.replace(/ /g, '-'))
  if (text.trim() !== '') p.set('q', text)
  return p.toString()
}

function RunList({ first, go, view = null, names = null, want }: {
  first: IssueRunSummaryPage; go: (to: string) => void; view?: string | null
  names?: ScheduleNames; want?: () => void
}) {
  const now = useNow()
  const scheduled = first.runs.some((r) => scheduleOf(r.created_by) !== null)
  useEffect(() => { if (scheduled) want?.() }, [scheduled])
  const [which, setWhich] = useState<RunFilter>(() => runListQuery(view).which)
  const [text, setText] = useState(() => runListQuery(view).text)
  // Written in place, never pushed: a chip or a keystroke is not a page.
  useEffect(() => {
    if (window.location.pathname !== '/runs') return
    const q = runListQueryString(which, text)
    const want = `/runs${q === '' ? '' : `?${q}`}`
    if (window.location.pathname + window.location.search !== want) window.history.replaceState(window.history.state, '', want)
  }, [which, text])
  const [older, setOlder] = useState<{ runs: IssueRunSummary[]; next: string | null; error: ApiError | null; reading: boolean }>(
    { runs: [], next: first.next_page_token, error: null, reading: false },
  )
  // A poll or refresh replaced the first page. With nothing appended, the
  // new page's token is the way on; with older runs shown, they stay (the
  // minute's poll must not take back what "Show older runs" read, RT-11),
  // less any the new first page now holds.
  useEffect(() => setOlder((o) => {
    if (o.runs.length === 0) return { runs: [], next: first.next_page_token, error: null, reading: false }
    const onFirst = new Set(first.runs.map((r) => r.id))
    return { ...o, runs: o.runs.filter((r) => !onFirst.has(r.id)) }
  }), [first])

  async function more() {
    if (older.next === null) return
    setOlder((o) => ({ ...o, reading: true, error: null }))
    const r = await loadRuns(older.next)
    if (r.status === 'ok' || r.status === 'stale') {
      setOlder((o) => ({ runs: [...o.runs, ...r.data.runs], next: r.data.next_page_token, error: null, reading: false }))
    } else if (r.status === 'empty') {
      setOlder((o) => ({ ...o, next: null, reading: false }))
    } else if (r.status === 'error') {
      setOlder((o) => ({ ...o, error: r.error, reading: false }))
    }
  }

  // In the order served: the API orders newest first, and this does not re-sort.
  const rows = [...first.runs, ...older.runs]
  const counts = Object.fromEntries(RUN_FILTERS.map((f) => [f, rows.filter((r) => RUN_FILTER_TEST[f](r)).length])) as Record<RunFilter, number>
  const shown = rows.filter((r) => RUN_FILTER_TEST[which](r) && runMatches(r, text))
  const filtering = which !== 'all' || text.trim() !== ''
  return (
    <div className="rn-list">
      {rows.length === 0 ? (
        // AN EMPTY STATE, NOT "NO RUN OF THE 0 READ MATCHES" (visual QA V040):
        // a first page that holds no run but says older ones exist reaches the
        // list rather than `Screen`'s own empty state. It says so, with the
        // way to start a run, and keeps "Show older runs" below it.
        <Absent
          kind="zero"
          heading="No runs yet"
          say={older.next === null ? 'No run has been read for this tenant.' : 'The newest page of runs holds none; older runs may exist.'}
          className="rn-empty"
        >
          {older.next === null ? 'This tenant has not planned an issue.' : 'The newest page holds no run; older ones may exist.'}{' '}
          <SubmitIssueLink go={go} /> starts one.
        </Absent>
      ) : (
        <>
          {/* FILTER AND SEARCH (QA G2-18, 2026-10-07): over the runs read so far.
              The API serves the list newest first with no filter, so an older run
              is found once "Show older runs" has read it -- the note says so. */}
          <div className="rn-filters">
            <Segmented
              label="Which runs"
              value={which}
              options={RUN_FILTERS.map((f) => ({ key: f, label: RUN_FILTER_LABEL[f], title: RUN_FILTER_TITLE[f], count: counts[f] }))}
              onChange={setWhich}
            />
            <input
              className="rn-search"
              type="search"
              aria-label="Find a run by issue or title"
              placeholder="Find a run by issue or title"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            {filtering && older.next !== null && (
              <span className="sb-note">Filters the {pluralise(rows.length, 'run')} read; older runs are not searched until shown.</span>
            )}
          </div>
          <p className="sb-note rn-count" role="status">
            {filtering ? `${shown.length} of ` : ''}{pluralise(rows.length, 'run')} read · {older.next === null ? 'no older runs' : 'older runs exist'}
          </p>
          {shown.length === 0 && (
            <p className="sb-note rn-none">No run of the {rows.length} read matches{text.trim() === '' ? '' : ` “${text.trim()}”`}{which === 'all' ? '' : ` in ${RUN_FILTER_LABEL[which]}`}.</p>
          )}
          {/* NO BARE HEADER (visual QA V040): with no row to show, the table's
              header row over nothing is not drawn. */}
          {shown.length > 0 && <div className="rn-table-wrap">
            {/* FIXED COLUMNS (browser QA D11): the table was 1085px in a 1056px
                card and cut the By column mid-address. Each id and name is one
                line, cut, whole in its title; the issue leads with its title. */}
            <table className="rn-table">
              <thead>
                <tr>
                  <th scope="col" data-col="run">Run</th>
                  <th scope="col" data-col="issue">Issue</th>
                  <th scope="col" data-col="state">State</th>
                  <th scope="col" data-col="approval">Plan approval</th>
                  <th scope="col" data-col="workflow">Workflow</th>
                  <th scope="col" data-col="created">Created</th>
                  <th scope="col" data-col="by">By</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.id} className="rn-row">
                    {/* THE CUT IS ON A SPAN, NOT THE CELL (visual QA V036): `.rn-cut`
                        is `display: block`, and on the `<td>` it took the Run cell
                        out of the table's row, ~16px off the others on a tall row. */}
                    <td data-label="Run"><span className="rn-cut" title={r.id}><InApp go={go} to={runAddress(r.id)} href={`/runs/${encodeURIComponent(r.id)}`}>{r.id}</InApp></span></td>
                    <td data-label="Issue">
                      {r.issue_read?.title ? (
                        <>
                          <span className="rn-cut rn-issue-t" title={r.issue_read.title}>{r.issue_read.title}</span>
                          <span className="mono rn-refcut rn-issue-ref" title={r.issue.ref}><IssueRefLink run={r} /></span>
                        </>
                      ) : (
                        <>
                          <span className="mono rn-refcut" title={r.issue.ref}><IssueRefLink run={r} /></span>
                          <NoTitle run={r} />
                        </>
                      )}
                    </td>
                    <td data-label="State">
                      <RunStateMark state={r.state} />
                      {runWhyLine(r) !== null && (
                        <span className="sb-note rn-cut rn-why" data-why={r.state} title={runWhyLine(r) ?? ''}>{runWhyLine(r)}</span>
                      )}
                    </td>
                    <td data-label="Plan approval" title={r.plan_approval === 'auto' ? 'This run\'s plan is approved automatically.' : 'This run\'s plan needs a person\'s approval.'}>{planApprovalWord(r)}</td>
                    {r.workflow_id === null ? (
                      /* THE NOTE IS CUT IN ITS COLUMN (visual QA VQ-017, 2026-10-26): the
                         italic 'none · the run ended before a workflow' ran over Created
                         at 1440 ('a wor7h ago'). Cut as every other cell's value is,
                         whole in its title; on a phone it wraps (intake.css). */
                      <td data-label="Workflow"><i className="ctl-em rn-cut rn-nowf" title={noWorkflowWord(r)}>{noWorkflowWord(r)}</i></td>
                    ) : (
                      <td data-label="Workflow">
                        <span className="mono rn-cut" title={r.workflow_id}>
                          <InApp go={go} to={`work/workflows?${new URLSearchParams({ wf: r.workflow_id }).toString()}`}
                            href={`/workflows/${encodeURIComponent(r.workflow_id)}`}>{r.workflow_id}</InApp>
                        </span>
                      </td>
                    )}
                    <td data-label="Created" title={r.created_at}>{timeAgo(r.created_at, now)}</td>
                    <td data-label="By">{runCreatorWord(r, names) !== null ? <span className="rn-cut" title={scheduleOf(r.created_by) === null ? runCreatorWord(r, names) ?? '' : `${runCreatorWord(r, names)} (${r.created_by})`} data-sweep={r.created_by === SWEEP_CREATOR || undefined}>{runCreatorWord(r, names)}</span> : <i className="ctl-em">&mdash; not recorded</i>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}
        </>
      )}
      {older.error !== null && <FailedPanel error={older.error} onRetry={() => void more()} />}
      {older.next !== null && (
        <p className="rn-more">
          <Button disabled={older.reading} onClick={() => void more()}>
            {older.reading ? 'Reading older runs…' : 'Show older runs'}
          </Button>
        </p>
      )}
    </div>
  )
}

/**
 * A RUN WITH NO WORKFLOW, IN WORDS THAT FIT ITS STATE (live QA RT-16): a run
 * that ended in planning, not ready or rejected said `none yet`, which
 * promises a workflow that will never come.
 */
export function noWorkflowWord(run: IssueRunSummary): string {
  return run.terminal ? 'none · the run ended before a workflow' : 'none yet'
}

/**
 * The issue's reference, linked to the issue on the forge (live QA RT-12: it was plain text).
 *
 * THE NUMBER IS NEVER THE PART CUT (visual QA V205, 2026-10-16). A
 * long `owner/repo#N` in a list cell was ellipsised from its end, and the
 * `#N` that tells one issue of a repository from another went first. The
 * repository is cut and the number is kept whole after it (`.rn-refcut`).
 */
function IssueRefLink({ run }: { run: IssueRunSummary }) {
  const url = run.issue.url
  const ref = run.issue.ref
  const at = ref.lastIndexOf('#')
  const text = at > 0
    ? <><span className="rn-refcut-repo">{ref.slice(0, at)}</span><span className="rn-refcut-n">{ref.slice(at)}</span></>
    : <span className="rn-refcut-repo">{ref}</span>
  return typeof url === 'string' && /^https:\/\//.test(url)
    ? <a href={url} target="_blank" rel="noreferrer">{text}</a>
    : <span>{text}</span>
}

// ---------------------------------------------------------------------------
// one run
// ---------------------------------------------------------------------------

/** Where a PLANNED run's actions are drawn: the waiting banner, or the plan's sticky foot. */
type ActionsAt = 'top' | 'foot'

type Acting =
  | { kind: 'idle' }
  | { kind: 'busy'; what: 'approve' | 'edit' | 'reject' }
  | { kind: 'failed'; error: ApiError }

/** What the run's state means, in one line under it. */
/**
 * `IssueRun.to_api().outcome` (#646): how a DONE run ended beyond its state.
 * Read off the served run rather than added to `IssueRun` in types.ts, which
 * this lane does not edit; absent from servers before #646.
 */
type RunOutcomeField = { outcome?: string | null }

/**
 * The outcome label (#646): "Already on main" for a run whose build found the
 * issue's work already on the default branch -- DONE, no pull request, the
 * build's verification table on the issue. Null for every other run.
 */
export function runOutcomeLabel(run: IssueRun): string | null {
  return (run as IssueRun & RunOutcomeField).outcome === 'already_on_main' ? 'Already on main' : null
}

/** The outcome beside the state pill; nothing when the run has none. */
export function RunOutcome({ run }: { run: IssueRun }) {
  const label = runOutcomeLabel(run)
  return label === null ? null : (
    <span className="sb-note" data-outcome="already_on_main"
      title="The build changed nothing: the work was already on the default branch (outcome already_on_main).">
      {label}
    </span>
  )
}

export function runStateLine(run: IssueRun, merge: MergeCard | null = null): string {
  switch (run.state) {
    case 'PLANNING':
      return 'A planner agent is reading the issue and the repository. It changes nothing.'
    case 'PLANNED':
      return run.plan_approval === 'auto'
        ? 'Holds no capacity. Approval is automatic: the next read approves this plan.'
        : 'Holds no capacity. Waiting for a person.'
    case 'APPROVED':
      return 'Approved; the workflow is being submitted.'
    case 'RUNNING':
      return 'The workflow built from the approved plan is running.'
    case 'CHECKING':
      return 'The pull request is open. Holds no capacity: its CI is read until it is green or red.'
    case 'FIXING':
      return `CI was red: fix round ${run.ci_fix_round ?? 1} of ${run.fix_rounds} is pushing to the pull request.`
    case 'DONE':
      // #646: no pull request, because nothing needed changing. The issue is
      // closed only when the build's table says every requirement is met.
      if (runOutcomeLabel(run) !== null) {
        return run.requirements_met === true
          ? 'The work was already on main: the build changed nothing, and its verification table on the issue shows every planned requirement met, so the issue was closed.'
          : 'The work was already on main: the build changed nothing. Its verification table is on the issue, which stays open: not every planned requirement was shown met.'
      }
      // The CI loop ends a run DONE when its PR merged OR when every required
      // check is green; `pull_request.merged` says which, and on a run stored
      // before it was served `mergeKnown` settles what the rest can.
      {
        const known = mergeKnown(run, merge).kind
        if (known === 'merged') {
          return run.green_sha
            ? 'The pull request merged, with every required check green.'
            : 'The pull request merged, which ended the run before its checks were green.'
        }
        if (known === 'not-merged') {
          return 'Every required check is green on the pull request. It is not merged.'
        }
      }
      return run.green_sha
        ? 'Every required check is green on the pull request. Its merge is not reported by this run.'
        : run.pull_request
          ? 'The pull request ended the run before its checks were green. Whether it merged is not reported by this run.'
          : 'The workflow succeeded.'
    case 'FAILED':
      return 'The run failed.'
    case 'REJECTED':
      return 'The plan was turned down. Nothing ran.'
    case 'CANCELLED':
      return 'The run was cancelled.'
    case 'NOT_READY':
      return 'The planner found the issue not ready, and said why on the issue. Nothing ran; it is planned again only after the issue is edited or commented on.'
  }
}

/**
 * Whether a fresh read replaced the plan the reader is looking at while it can
 * still be acted on. A plan first appearing (PLANNING -> PLANNED) and a run that
 * left PLANNED are drawn at once: neither leaves an action naming stale text.
 */
export function planMovedUnder(shown: IssueRun, served: IssueRun): boolean {
  return shown.plan_digest !== null && served.state === 'PLANNED' && served.plan_digest !== shown.plan_digest
}

function RunPage({ run: served, reread, go, onHeading, names = null, want }: {
  run: IssueRun
  reread: (why: string) => void
  go: (to: string) => void
  /** Told the page's title: the issue's, or its reference when the run kept none. */
  onHeading: (title: string) => void
  /** The schedules' names, read once a schedule is the run's creator or in its history. */
  names?: ScheduleNames
  want?: () => void
}) {
  const now = useNow()
  const scheduled = [served.created_by, ...served.history.map((h) => h.by)].some((b) => scheduleOf(b) !== null)
  useEffect(() => { if (scheduled) want?.() }, [scheduled])
  // THE ANSWER TO AN ACTION IS DRAWN AT ONCE: the API returns the run it
  // moved, and a poll that lands later replaces it with whatever it reads.
  const [run, setRun] = useState(served)
  // A POLL NEVER SWAPS THE PLAN UNDER THE READER. Approve, Edit and Reject
  // carry the digest of the plan on screen (D3); if a 15s poll replaced that
  // plan silently, the next click would carry a digest for text nobody read,
  // and an editor opened on the old plan would overwrite the new one. So a
  // read that serves a different digest for a still-PLANNED run is held, and
  // a notice offers it; the plan on screen stays the one the actions name.
  const [held, setHeld] = useState<IssueRun | null>(null)
  const shown = useRef(run)
  shown.current = run
  /**
   * A DECISION SHOWS ITS OUTCOME WHERE IT WAS TAKEN (QA1-FIX-24,
   * runs-submit-03, part of #2596). After Approve and run, and after Reject
   * this plan, the page read "Planned · Waiting for a person" again with the
   * buttons offered: the answer was drawn, then a read that still served the
   * plan as Planned -- the 15s poll, or the read a tab makes when it is shown
   * again -- drew the undecided run back over it. A run never returns to
   * Planned on the plan it was decided on, so such a read is older than the
   * decision and is not drawn; a line beside where the decision was taken
   * says what it did, and a refusal is drawn there too, not under the risks.
   */
  const [decided, setDecided] = useState<{ what: 'approve' | 'reject'; digest: string | null; at: ActionsAt } | null>(null)
  const decidedRef = useRef(decided)
  decidedRef.current = decided
  useEffect(() => {
    const d = decidedRef.current
    if (d !== null && served.state === 'PLANNED' && served.plan_digest === d.digest) return
    if (planMovedUnder(shown.current, served)) {
      setHeld(served)
    } else {
      setHeld(null)
      setRun(served)
    }
  }, [served])
  const title = run.issue_read?.title || run.issue.ref
  // The repository's index and the pull request's impact (screens 5 and 10), read once for both cards.
  const indexRead = useRunIndex(run)
  // On the title alone: `onHeading` is a new function on every render of the screen.
  useEffect(() => onHeading(title), [title])
  const [acting, setActing] = useState<Acting>({ kind: 'idle' })
  /** Where the last action was pressed: its refusal is drawn there. */
  const [actedAt, setActedAt] = useState<ActionsAt>('top')
  const pageRef = useRef<HTMLDivElement>(null)
  // The editor records the digest it opened on, and saves with that one.
  // `at`: which of the two action rows asked to reject, so its form opens where the reader is.
  const [open, setOpen] = useState<{ kind: 'none' } | { kind: 'edit'; digest: string } | { kind: 'reject'; at: ActionsAt }>(
    { kind: 'none' },
  )
  const close = () => setOpen({ kind: 'none' })
  const showHeld = () => {
    if (held === null) return
    setRun(held)
    setHeld(null)
    close()
  }

  const canAct = run.state === 'PLANNED' && run.plan_digest !== null && run.plan !== null
  const busy = acting.kind === 'busy'

  async function act(what: 'approve' | 'edit' | 'reject', at: ActionsAt, call: () => Promise<Result<unknown>>) {
    setActing({ kind: 'busy', what })
    setActedAt(at)
    const on = run.plan_digest
    const r = await call()
    if (r.status === 'ok' || r.status === 'stale') {
      const next = (r.data as { run?: IssueRun } | null)?.run
      setActing({ kind: 'idle' })
      close()
      setHeld(null)
      if (what !== 'edit') setDecided({ what, digest: on, at })
      if (next !== undefined && next !== null && typeof next.id === 'string') setRun(next)
      else reread('The API accepted this and did not echo the run, so it was read again.')
      return
    }
    if (r.status !== 'error') return
    if (r.error.code === 'plan_changed') {
      setActing({ kind: 'idle' })
      close()
      reread(
        'The plan changed since you opened it, so nothing was done. This is the plan now: read it, then act on it again.',
      )
      return
    }
    if (r.error.code === 'invalid_run_transition' || r.error.kind === 'conflict') {
      setActing({ kind: 'idle' })
      close()
      reread(`The run moved on since you opened it, so nothing was done: ${r.error.message}`)
      return
    }
    setActing({ kind: 'failed', error: r.error })
  }

  const digest = run.plan_digest
  // THE RUN'S WORKFLOWS, READ EACH TIME THE RUN IS (lane U14): the Steps
  // card, the plan's live states, Progress, the cost and the PR the
  // integrator opened all come from this one read.
  const read = useRunWorkflows(run)
  const wfPr = wfPrOf(run, read)
  const rows = stepRows(run, read)
  const merge = runMergeCard(rows)
  const progress = progressEntries(run, rows)
  const approved = planApproved(run)
  // From CHECKING the pull request is the subject: its card leads (item 5).
  const prLeads = lead(run)
  /** Approve, Edit and Reject -- or the rejection form, where it was asked for. Null while editing. */
  const actions = (at: ActionsAt): ReactNode => {
    if (!canAct || open.kind === 'edit') return null
    if (open.kind === 'reject' && open.at === at) {
      return (
        <RejectForm busy={busy} onCancel={() => keep(at)}
          onReject={(reason) => void act('reject', at, () => rejectPlan(run.id, digest, reason))} />
      )
    }
    return (
      <div className="rn-actions">
        <Button kind="primary" disabled={busy}
          onClick={() => void act('approve', at, () => approvePlan(run.id, digest!))}>
          {acting.kind === 'busy' && acting.what === 'approve' ? 'Approving…' : 'Approve and run'}
        </Button>
        <Button disabled={busy} onClick={() => setOpen({ kind: 'edit', digest: digest! })}>Edit plan</Button>
        <Button disabled={busy} data-act={`reject-${at}`} onClick={() => setOpen({ kind: 'reject', at })}>Reject</Button>
      </div>
    )
  }
  /**
   * "Keep it", or Escape in the reject form: the form closes and the Reject
   * that opened it has the focus again (QA1-FIX-24, runs-submit-08), not the
   * page's <body>, so a keyboard reader goes on from where they were.
   */
  function keep(at: ActionsAt) {
    close()
    requestAnimationFrame(() => pageRef.current?.querySelector<HTMLElement>(`[data-act="reject-${at}"]`)?.focus())
  }
  /** What a decision did, or why it was refused, drawn beside the buttons pressed. */
  const answer = (at: ActionsAt): ReactNode => {
    if (acting.kind === 'failed' && actedAt === at) {
      return <FailedPanel error={acting.error} onRetry={() => setActing({ kind: 'idle' })} />
    }
    if (decided === null || decided.at !== at || run.state === 'PLANNED') return null
    return (
      <p className="rn-decided sb-note" role="status">
        {decided.what === 'approve'
          ? <><b>Approved.</b> The run is {runStateWord(run.state)} now{run.workflow_id === null ? '.' : ', with its workflow created.'}</>
          : <><b>Rejected.</b> The run is {runStateWord(run.state)} now; nothing ran.</>}
      </p>
    )
  }
  return (
    <div className="rn-page" ref={pageRef}>
      <section className="rn-main">
        <div className="rn-state">
          <RunStateMark state={run.state} />
          <RunOutcome run={run} />
          <span className="rn-state-t">{runStateLine(run, merge)}</span>
          {run.state === 'PLANNED' && <span className="sb-note">planned {timeAgo(run.updated_at, now)}</span>}
          {run.approved_by !== null && (
            <span className="sb-note">
              approved {run.approved_at === null ? '' : timeAgo(run.approved_at, now)} by{' '}
              {run.approved_by === AUTO_APPROVER ? 'automatic approval' : run.approved_by}
            </span>
          )}
        </div>

        {run.error !== null && (
          // `ended FAILED` is the API's sentence, stored on the run, so its
          // state tokens are worded at read (QA G2-25, `proseWords`).
          <p className="rn-error" role="alert"><b>Why:</b> {proseWords(run.error)}</p>
        )}
        {prLeads && <CiCard run={run} go={go} wfPr={wfPr} index={indexRead} merge={merge} />}
        <StepsCard run={run} read={read} go={go} now={now} />
        {run.state === 'NOT_READY' && (
          <section className="rn-card rn-not-ready" aria-label="Not ready">
            <h3>Not ready{run.not_ready?.kind ? ` · ${run.not_ready.kind.replace(/_/g, ' ')}` : ''}</h3>
            <p>{run.not_ready?.reason ?? 'The planner gave no reason.'}</p>
            {(run.not_ready?.needs ?? []).length > 0 && (
              <ul>{(run.not_ready?.needs ?? []).map((need, i) => <li key={i}>{need}</li>)}</ul>
            )}
          </section>
        )}
        {run.state === 'PLANNED' && run.hold && (
          <p className="sb-note" data-hold="">
            <b>Waiting:</b> <code>{run.hold}</code> &mdash; another live run&rsquo;s plan edits the same files; this one is approved once that run ends.
          </p>
        )}
        {run.state === 'REJECTED' && (
          <p className="sb-note">
            Rejected by {run.rejected_by ?? '—'}
            {run.rejection_reason ? `: ${run.rejection_reason}` : ' · no reason given'}
          </p>
        )}

        {/* THE ACTIONS ARE AT THE TOP (owner QA D, 2026-10-04): they were
            only at the very foot, under the overlaps, the plan and the
            requirements. The banner carries them; the plan's own foot
            carries them again, in a bar that sticks to the bottom edge. */}
        {canAct && (
          <div className="rn-wait">
            <p className="rn-wait-t">
              {run.plan_approval === 'required' ? (
                <><b>Waiting for a person.</b> Anyone in tenant <code>{run.tenant_id}</code> may approve, edit or reject.</>
              ) : (
                <><b>Approval is automatic:</b> the next read approves this plan. Anyone in tenant <code>{run.tenant_id}</code> may act on it first.</>
              )}{' '}
              Approving carries this plan&rsquo;s digest, so a plan edited by someone else since you opened it is refused
              rather than run.
            </p>
            {actions('top')}
            {answer('top')}
          </div>
        )}
        {!canAct && answer('top')}

        {held !== null && (
          <div className="rn-notice rn-held" role="status">
            <WarnMark />
            <span>
              The plan changed since this page drew it
              {held.plan_edited_by !== null && <> (edited by {held.plan_edited_by})</>}. The actions below still name the
              plan shown, so the API will refuse them.
            </span>
            <Button disabled={busy} onClick={showHeld}>Show the plan now</Button>
          </div>
        )}

        {run.writeback_error && (
          <div className="rn-writeback">
            <Banner tone="warn" title="The issue on GitHub was not updated">
              {run.writeback_error} · the run carries on; the next read tries the write again.
            </Banner>
          </div>
        )}

        <RunContextCard run={run} ctx={indexRead} go={go} />

        {run.plan !== null && <Overlaps plan={run.plan} openWork={run.open_work ?? null} folded={approved} ended={run.terminal} />}

        <section className="rn-plan" aria-label="The plan">
          <Fold folded={approved && run.plan !== null}
            line={run.plan === null ? null : <span className="sb-note">{plannedSteps(run.plan)} · approved</span>}
            head={
              <h3>
                The plan
                {digest !== null && <> · <code className="rn-digest" title={digest}>{shortDigest(digest)}</code></>}
              </h3>
            }>
          {run.plan === null ? (
            <p className="sb-note">
              {run.state === 'PLANNING'
                ? 'No plan yet: the planner agent is still working.'
                : 'No plan: the planner did not produce one this run could read.'}
            </p>
          ) : open.kind === 'edit' && canAct ? (
            <PlanEditor
              key={open.digest}
              plan={run.plan}
              busy={busy}
              onCancel={close}
              onSave={(plan) => {
                const opened = open.digest
                void act('edit', 'foot', () => editPlan(run.id, opened, plan))
              }}
            />
          ) : (
            <>
              <p className="sb-note">
                revision {run.plan_revision}
                {run.plan_edited_by !== null && <> · edited by {run.plan_edited_by}</>}
                {' · '}{run.plan_shape ?? `${pluralise(run.plan.steps.length, 'step')}, then a review and a fix`}
                {' '}gated on its verdict · every step runs as claude-code
              </p>
              <p className="rn-plan-facts">
                {/* A PLAN WITH NO MODE SAYS NOTHING OF IT (QA2-FIX-20, runs2-11): `mode
                    not stated` named a field the reader cannot act on; the steps
                    under it say what runs. */}
                {(run.plan.mode === 'single' || run.plan.mode === 'workflow') &&
                  <Chip title="The planner's call: one agent, or a workflow of several steps">{run.plan.mode === 'single' ? 'single agent' : 'workflow'}</Chip>}
                {run.plan.estimate
                  ? <Chip title="The planner's estimate for the whole plan">estimate {run.plan.estimate}</Chip>
                  : <span className="sb-note">no estimate given</span>}
              </p>
              <PlanBody plan={run.plan} unmet={run.requirements_unmet ?? []}
                live={approved ? { read, rows, go } : null} />
            </>
          )}
          {/* THE CONFIRM OPENS IN FLOW (QA1-FIX-24, runs-submit-29, part of
              #2596): opened in the sticky bar, the reject form grew the bar
              up over the plan it was turning down. While it is open the foot's
              actions are a plain block under the plan; the bar (opaque, with
              its top border, intake.css) carries only the three buttons. */}
          {canAct && open.kind !== 'edit' && (
            <div className={open.kind === 'reject' && open.at === 'foot' ? 'rn-actflow' : 'rn-actbar'}>{actions('foot')}{answer('foot')}</div>
          )}
          {open.kind === 'edit' && answer('foot')}
          </Fold>
          {!canAct && answer('foot')}
        </section>

        {!prLeads && <CiCard run={run} go={go} wfPr={wfPr} index={indexRead} merge={merge} />}
        {textList(run.plan?.risks) !== null && (
          <section className="rn-risks" aria-label="Risks">
            <h3>Risks the planner named</h3>
            <ul className="rn-list-items">
              {textList(run.plan?.risks)!.map((r, i) => <li key={i}><RnText text={r} /></li>)}
            </ul>
          </section>
        )}

        <section className="rn-history" aria-label="History">
          <h3>Progress</h3>
          {/* THE STEPS' CHANGES UNDER THE RUN'S, OLDEST FIRST (item 3). */}
          <ol>
            {progress.map((h, i) => h.kind === 'step' ? <StepProgress key={h.key} e={h} prev={previousAt(progress, i)} now={now} /> : (
              <li key={h.key}>
                <RunStateMark state={h.to} />
                <span>{h.from === null ? 'created' : h.from === 'PLANNED' && h.to === 'PLANNED' ? `plan edited · revision ${planRevisionAt(progress, i)}` : `from ${stateWord(h.from)}`} · by {h.by ? <span title={scheduleOf(h.by) === null ? undefined : h.by}>{byWord(h.by, names)}</span> : '—'}</span>
                <ProgressTime at={h.at} prev={previousAt(progress, i)} now={now} />
              </li>
            ))}
          </ol>
          {historyGap(run) !== null && <p className="sb-note rn-hgap-note" data-gap="">{historyGap(run)}</p>}
        </section>
      </section>

      <aside className="rn-side">
        <section className="rn-card rn-links" aria-label="Linked">
          <h3>Linked</h3>
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>Issue</b>
              <IssueLink run={run} />
            </li>
            <CommentFact label="Plan comment" name="plan comment" url={commentUrl(run, run.plan_comment_id)}
              absent={run.plan === null ? 'not posted · there is no plan yet' : 'not posted yet'} />
            <CommentFact label="Status comment" name="status comment" url={commentUrl(run, run.status_comment_id)}
              absent="not posted yet" />
            <li className="ctl-fact">
              <b>Planner</b>
              {run.planner_task_id === ''
                ? <i className="ctl-em" title="The run records no planner agent.">&mdash; not recorded</i>
                : <InApp go={go} words to={`work/task/${encodeURIComponent(run.planner_task_id)}`}
                  href={`/agents/recent/${encodeURIComponent(run.planner_task_id)}`}>planner agent</InApp>}
              {/* THE LINK NAMES THE PLANNER (QA1 runs-submit-27): its raw id was the
                  link's text. The link is words now, so it wraps; the id follows
                  on its own row as a note, cut to the card as the link was
                  (`.rn-id`, D3), whole in its title. */}
              {run.planner_task_id !== '' && <span className="sb-note rn-id" title={run.planner_task_id}>{run.planner_task_id}</span>}
            </li>
            <li className={run.workflow_id === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>Workflow</b>
              {run.workflow_id === null
                ? <i className="ctl-em" title={run.terminal ? 'None: the run ended before its plan was approved, so no workflow was created.' : 'None yet: the workflow is created when the plan is approved.'}>{run.terminal ? noWorkflowWord(run) : 'none yet · created on approval'}</i>
                : <InApp go={go} cut to={`work/workflows?${new URLSearchParams({ wf: run.workflow_id }).toString()}`}
                  href={`/workflows/${encodeURIComponent(run.workflow_id)}`}>{run.workflow_id}</InApp>}
            </li>
            <PullRequestFact run={run} wfPr={wfPr} />
          </ul>
        </section>
        <IssueReadCard run={run} now={now} />
        <section className="rn-card rn-chosen" aria-label="Chosen at submission">
          <h3>Chosen at submission</h3>
          <ul className="ctl-facts">
            <li className="ctl-fact"><b>plan approval</b><span>{run.plan_approval}</span></li>
            <li className="ctl-fact"><b>auto-merge</b><span>{run.auto_merge ? 'on' : 'off'}</span></li>
            <li className="ctl-fact"><b>fix rounds</b><span>up to {run.fix_rounds}</span></li>
            <li className="ctl-fact"><b>by</b>{runCreatorWord(run, names) !== null
              ? <span className="rn-id" title={scheduleOf(run.created_by) === null ? runCreatorWord(run, names) ?? '' : `${runCreatorWord(run, names)} (${run.created_by})`}>{runCreatorWord(run, names)}</span>
              : <i className="ctl-em">&mdash; not recorded</i>}</li>
            <CostFact read={read} ended={run.terminal} />
          </ul>
        </section>
      </aside>
    </div>
  )
}

/**
 * A TRANSITION MISSING FROM THE RECORD IS SAID (QA1-FIX-24, runs-submit-06,
 * part of #2596): Progress ended at "Planning → Planned" under a Running
 * badge that said "approved 9d ago", and nothing said the approval and the
 * start were not in the history. When the last transition recorded does not
 * reach the run's state, a line under the last row says so.
 */
export function historyGap(run: IssueRun): string | null {
  const last = run.history[run.history.length - 1]
  if (last === undefined) return `No transition is recorded; the run is ${runStateWord(run.state)} now.`
  if (last.to === run.state) return null
  return `The record ends at ${runStateWord(last.to)}; the run is ${runStateWord(run.state)} now. The transitions between were not recorded.`
}

/** A list as the editor shows it: one entry per line. */
function linesOf(list: string[] | null | undefined): string {
  return (list ?? []).join('\n')
}

/** The entries a textarea holds: one per non-blank line, trimmed. */
function entries(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter((l) => l !== '')
}

/**
 * An optional plan field as edited. Untouched, it is the value the plan had --
 * absent, null or a list -- so a save changes nothing the reader did not
 * change; emptied, it is dropped (the API takes no empty estimate); otherwise
 * it is what was typed.
 */
function editedList(original: string[] | null | undefined, text: string): string[] | null | undefined {
  if (text === linesOf(original)) return original
  const l = entries(text)
  return l.length === 0 ? undefined : l
}

function editedText(original: string | null | undefined, text: string): string | null | undefined {
  if (text === (original ?? '')) return original
  return text.trim() === '' ? undefined : text.trim()
}

/** `target[key] = value`, or no key at all when the value is undefined. */
function put<T extends object, K extends keyof T>(target: T, key: K, value: T[K] | undefined): void {
  if (value === undefined) delete target[key]
  else target[key] = value
}

/**
 * `owner/repo#N`, linked. It wrapped at the owner's hyphen ('bogdan- /
 * alexandrescu/SwarmCloud#454', item 6), then was cut to one line past the
 * card's edge (owner QA A, 2026-10-04). It takes the card's whole width under
 * its label and breaks only after `/` or `#`; its whole text is the title.
 */
/** Where an issue reference may wrap: after `/` or `_`, and before the `#` so the number keeps it. */
export const ISSUE_REF_BREAKS = /[/_]|(?=#)/g

function IssueLink({ run }: { run: IssueRun }) {
  return (
    <a href={run.issue_read?.url || run.issue.url} target="_blank" rel="noreferrer" className="mono rn-ref" title={run.issue.ref} aria-label={run.issue.ref}>
      {/* BEFORE THE `#`, NOT AFTER IT (visual QA V107): a break after `#`
          left the number alone on the next line. `#454` is one piece. */}
      {breakAt(run.issue.ref, ISSUE_REF_BREAKS)}
    </a>
  )
}

/** A plan's optional list (`risks`, a step's `files` or `tests`): its strings, or null when it has none. */
export function textList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  const items = value.filter((v): v is string => typeof v === 'string' && v.trim() !== '')
  return items.length === 0 ? null : items
}

/** The most a plan's lead may run to, in RENDERED characters: two or three lines in the plan's column. */
export const PLAN_LEAD_CHARS = 240

/** What a run of plan text draws: its code spans' backticks are markup, not text (`InlineText`). */
function rendered(text: string): string {
  return text.replace(/`([^`\n]+)`/g, '$1')
}

/**
 * The plan's summary cut to its lead (item 5): its first sentences, up to
 * three and `PLAN_LEAD_CHARS`, and whether anything was left out. The whole
 * summary is one "Read more" away. A first sentence longer than the limit is
 * cut at a word with an ellipsis.
 *
 * CUT ON THE RENDERED TEXT (owner QA N18, 2026-10-04): the limit counted the
 * markdown source, and a cut inside `` `apps/…` `` left an unpaired backtick
 * that `InlineText` drew raw. A code span is now one word: the lead keeps it
 * whole or stops before it, and the limit counts what is drawn. A sentence
 * ends at `.`, `!` or `?` before a space, so `runs.py` is not two sentences.
 */
export function planLead(summary: string): { lead: string; cut: boolean } {
  const text = summary.trim().replace(/\s+/g, ' ')
  const sentences = text.split(/(?<=[.!?])\s+(?=[^\s])/)
  let lead = ''
  for (const sentence of sentences.slice(0, 3)) {
    const next = lead === '' ? sentence : `${lead} ${sentence}`
    if (lead !== '' && rendered(next).length > PLAN_LEAD_CHARS) break
    lead = next
  }
  if (rendered(lead).length > PLAN_LEAD_CHARS) {
    // Words, each code span whole inside the word it sits in.
    const words = lead.match(/(?:`[^`\n]+`|[^\s`]|`)+/g) ?? [lead]
    let kept = ''
    for (const w of words) {
      const next = kept === '' ? w : `${kept} ${w}`
      if (rendered(next).length > PLAN_LEAD_CHARS - 1) break
      kept = next
    }
    // One word longer than the whole lead: its drawn text, cut.
    if (kept === '') kept = rendered(words[0]!).slice(0, PLAN_LEAD_CHARS - 1)
    return { lead: oneEllipsis(kept), cut: true }
  }
  const cut = lead.length < text.length
  return { lead: cut ? oneEllipsis(lead) : lead, cut }
}

/**
 * ONE ELLIPSIS AT THE END OF A CUT LEAD (browser QA N18): a lead the planner
 * had already ended in `…` or `...` got a second one, "… …". Whatever trailing
 * dots or ellipsis it has become exactly one.
 */
function oneEllipsis(lead: string): string {
  return `${lead.replace(/[\s.…]+$/u, '').trimEnd()}…`
}

/**
 * THE PLAN FROM ITS SCHEMA (item 5): the lead, then the numbered steps --
 * title, step id, what it waits for, the files, tests and estimate when the
 * plan states them, and the prompt folded -- then the raw plan behind a
 * disclosure. On a phone the plan was one paragraph with no end.
 */
/** What a plan step needs to draw its live state once the run is approved; null before. */
type PlanLive = { read: RunWorkflows; rows: StepRow[]; go: (to: string) => void } | null

function PlanBody({ plan, unmet, live }: { plan: RunPlan; unmet: string[]; live: PlanLive }) {
  const { lead, cut } = planLead(plan.summary)
  // "READ MORE" BESIDE THE CUT (owner QA F, 2026-10-04): the lead ended in
  // `…` and the rest was behind "Show the full plan", under the steps.
  const [whole, setWhole] = useState(false)
  return (
    <>
      <p className="rn-summary">
        <RnText text={whole ? plan.summary.trim().replace(/\s+/g, ' ') : lead} />
        {cut && (
          <>
            {' '}
            <button type="button" className="rn-more-btn" aria-expanded={whole} onClick={() => setWhole((w) => !w)}>
              {whole ? 'Show less' : 'Read more'}
            </button>
          </>
        )}
      </p>
      <Requirements plan={plan} unmet={unmet} />
      <ol className="rn-steps">
        {plan.steps.map((s, i) => (
          <PlanStep key={s.step_id} step={s} n={i + 1} live={live} />
        ))}
      </ol>
      <details className="rn-raw">
        <summary>Show the full plan</summary>
        <p className="rn-full"><RnText text={plan.summary} /></p>
        <pre className="rn-raw-json">{JSON.stringify(plan, null, 2)}</pre>
      </details>
    </>
  )
}

/**
 * One plan step. BEFORE APPROVAL it says what it waits for ("starts at once",
 * "after api"); ONCE APPROVED (lane U14 item 2) "starts at once" is replaced
 * by the step's live state and its agent link -- a dependent step keeps
 * "after …" beside them.
 */
function PlanStep({ step, n, live }: { step: PlanStepDoc; n: number; live: PlanLive }) {
  const files = textList(step.files)
  const tests = textList(step.tests)
  const estimate = typeof step.estimate === 'string' && step.estimate.trim() !== '' ? step.estimate : null
  // THE ROLE ONCE (QA1-FIX-24, runs-submit-28, part of #2596): a title that
  // already opens with its step id ("api: …") had the id again, raw in mono,
  // on the line under it. The id line is drawn only for a title that does not
  // name it; what the step waits for keeps its line.
  const named = step.title.trim().toLowerCase().startsWith(step.step_id.toLowerCase())
  const deps = step.depends_on !== undefined && (live === null || step.depends_on.length > 0)
  return (
    <li className="rn-step">
      <b className="rn-step-h">{n} · <RnText text={step.title} /></b>
      {(!named || deps) && (
        <span className="rn-step-id sb-note">
          {!named && <span className="mono">{step.step_id}</span>}
          {deps && (
            <span className="rn-deps">
              {named ? '' : ' · '}
              {step.depends_on!.length === 0 ? 'starts at once' : <>after <span className="mono">{step.depends_on!.join(', ')}</span></>}
            </span>
          )}
        </span>
      )}
      {live !== null && (
        <PlanStepLive read={live.read} go={live.go}
          row={planStepRow(live.rows, step.step_id)} />
      )}
      {files !== null && <StepList label="Touches" items={files} />}
      {tests !== null && <StepList label="Tests" items={tests} />}
      {estimate !== null && <span className="rn-step-meta sb-note">{estimate}</span>}
      <details className="rn-prompt-d">
        <summary>Prompt</summary>
        <p className="rn-prompt"><RnText text={step.prompt} /></p>
      </details>
    </li>
  )
}

/** A path or test id breaks after `/`, `::` or `_`, never inside a name (owner QA C). */
const STEP_JOINTS = /::|[/_]/g

/**
 * A STEP'S TOUCHES OR TESTS AS A LIST (owner QA C, 2026-10-04): one comma
 * line broke anywhere ('…_is_rec / orded_on…'). One item per line, broken
 * only at a joint, each with a copy button -- a reader pastes these.
 */
function StepList({ label, items }: { label: 'Touches' | 'Tests'; items: string[] }) {
  return (
    <div className="rn-step-l">
      <b className="rn-sub">{label}</b>
      <ul className="rn-step-list" aria-label={label}>
        {items.map((item, i) => (
          <li key={`${i}-${item}`}>
            <code title={item}>{breakAt(item, STEP_JOINTS)}</code>
            <CopyText text={item} />
          </li>
        ))}
      </ul>
    </div>
  )
}

/** A copy button for one value; says so when the browser refused the clipboard. */
function CopyText({ text }: { text: string }) {
  const [said, setSaid] = useState<'copied' | 'refused' | null>(null)
  const copy = () => {
    // `navigator.clipboard` is undefined outside a secure context and a write can be denied.
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined) {
      setSaid('refused')
      return
    }
    clip.writeText(text).then(() => setSaid('copied'), () => setSaid('refused'))
  }
  return (
    <button type="button" className="rn-copy" aria-label={`Copy ${text}`}
      title={said === 'refused' ? 'This browser refused the clipboard: select the text instead.' : said === 'copied' ? 'Copied' : `Copy ${text}`}
      onClick={copy}>
      <CIcon name={said === 'copied' ? 'check' : 'copy'} />
    </button>
  )
}

/**
 * WHAT THE ISSUE SAID WHEN THE RUN WAS CREATED (item 4): the preview's read,
 * kept on the run (`issue_read`), masked and bounded as the preview is. A run
 * created before runs kept it, or whose read failed, says which, per fact --
 * never a blank, and never a 0 for comments it did not read.
 */
function IssueReadCard({ run, now }: { run: IssueRun; now: number }) {
  const read = run.issue_read ?? null
  const failed = run.issue_read_error ?? null
  // ONE SHORT LINE FOR WHAT WAS NOT KEPT (browser QA D11): the reason was
  // printed under each of five keys, a column of the same sentence.
  const why =
    failed !== null
      ? `Not read at submission: ${failed.message}`
      : 'Not kept: this run is older than the issue snapshot.'
  return (
    <section className="rn-card rn-read" aria-label="Read from the issue">
      <h3>Read from the issue</h3>
      <ul className="ctl-facts">
        <li className="ctl-fact"><b>issue</b><IssueLink run={run} /></li>
        {read === null ? null : (
          <>
            <li className="ctl-fact"><b>title</b><span title={read.title}><RnText text={read.title} /></span></li>
            {/* A READING, DATED (QA1-FIX-24, chrome-C14, part of #2596): a
                Done run's PR card said "closed by the merge" while this said
                "open" with no time, as if it were the issue now. The state is
                the one read at submission and says so; it is never re-read. */}
            <li className="ctl-fact"><b>state</b><span title="Read once, when the run was submitted; not read again since." data-state-then="">
              {read.state} · at submission
            </span></li>
            <li className="ctl-fact"><b>labels</b><span>{read.labels.length === 0 ? 'none' : breakAt(read.labels.join(' · '))}</span></li>
            <li className="ctl-fact">
              <b>body</b>
              <span>
                {read.body === '' ? 'empty' : `${read.body.length.toLocaleString('en-US')} chars`}
                {read.body_truncated && ' · cut at the preview\u2019s length'}
                {read.body_redacted && ' · masked'}
              </span>
            </li>
            <li className="ctl-fact"><b>comments</b><span>{read.comments}</span></li>
            <li className="ctl-fact">
              <b>read</b>
              <span title={read.read_at ?? 'The run recorded no time for this reading.'}>{read.read_at === null ? '— at submission · the time was not recorded' : `${timeAgo(read.read_at, now)}, at submission`}</span>
            </li>
          </>
        )}
      </ul>
      {read === null && (
        <p className="ctl-em rn-read-none" title="Title, state, labels, body and comments are not on this run.">
          {why}
        </p>
      )}
      {read !== null && read.body !== '' && (
        <details className="rn-read-body">
          <summary>Show the body</summary>
          {/* RENDERED, NOT RAW (live QA RT-18): the body read as `## Description`
              and `**…**`. The artifact viewer's renderer builds React elements,
              never HTML, from bytes nobody on this tenant wrote. */}
          <div className="rn-issue-md"><Markdown source={read.body} /></div>
        </details>
      )}
    </section>
  )
}

type StepDraft = { step: PlanStepDoc; title: string; prompt: string; files: string; tests: string; estimate: string }

/**
 * The plan, editable where a plan is: the summary, the estimate and the
 * requirements, and each step's title, prompt, files, tests and estimate.
 * The mode and the overlaps are the planner's findings and are kept as
 * written. Every field the plan carries goes back, so an edit never drops one.
 */
function PlanEditor({ plan, busy, onCancel, onSave }: {
  plan: RunPlan; busy: boolean; onCancel: () => void; onSave: (plan: RunPlan) => void
}) {
  const [summary, setSummary] = useState(plan.summary)
  const [estimate, setEstimate] = useState(plan.estimate ?? '')
  const [requirements, setRequirements] = useState(linesOf(plan.requirements))
  const [steps, setSteps] = useState<StepDraft[]>(plan.steps.map((s) => ({
    step: s, title: s.title, prompt: s.prompt, files: linesOf(s.files), tests: linesOf(s.tests), estimate: s.estimate ?? '',
  })))
  // WHAT BLOCKS SAVING IS NAMED (QA1-FIX-24, runs-submit-09, part of #2596):
  // Save went disabled with nothing saying why, and no field was marked. Each
  // blank required field is `aria-invalid` with its own hint, and the
  // disabled Save says which fields block it (`disabledReason`, its
  // aria-describedby).
  const blanks = [
    ...(summary.trim() === '' ? ['the summary'] : []),
    ...steps.flatMap((s, i) => [
      ...(s.title.trim() === '' ? [`step ${i + 1}'s title`] : []),
      ...(s.prompt.trim() === '' ? [`step ${i + 1}'s prompt`] : []),
    ]),
  ]
  const blank = blanks.length > 0
  // The editor opens on its first field, not on <body> (runs-submit-08).
  const first = useRef<HTMLTextAreaElement>(null)
  useEffect(() => first.current?.focus(), [])
  const hintId = useId()
  const set = (i: number, key: 'title' | 'prompt' | 'files' | 'tests' | 'estimate', value: string) =>
    setSteps((all) => all.map((s, j) => (j === i ? { ...s, [key]: value } : s)))

  function edited(): RunPlan {
    const out: RunPlan = {
      ...plan,
      summary,
      steps: steps.map((d) => {
        const step: PlanStepDoc = { ...d.step, title: d.title, prompt: d.prompt }
        put(step, 'files', editedList(d.step.files, d.files))
        put(step, 'tests', editedList(d.step.tests, d.tests))
        put(step, 'estimate', editedText(d.step.estimate, d.estimate))
        return step
      }),
    }
    put(out, 'estimate', editedText(plan.estimate, estimate))
    put(out, 'requirements', editedList(plan.requirements, requirements))
    return out
  }

  return (
    <form className="rn-edit" onSubmit={(e) => {
      e.preventDefault()
      if (!blank) onSave(edited())
    }}>
      <label className="rn-field">
        <b>Summary</b>
        <textarea ref={first} value={summary} rows={3} onChange={(e) => setSummary(e.target.value)}
          aria-invalid={summary.trim() === '' || undefined}
          aria-describedby={summary.trim() === '' ? `${hintId}-summary` : undefined} />
      </label>
      {summary.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-summary`}>Required: the plan needs a summary.</span>}
      <label className="rn-field">
        <b>Estimate</b>
        <input value={estimate} maxLength={200} onChange={(e) => setEstimate(e.target.value)} aria-describedby={`${hintId}-estimate`} />
      </label>
      {/* Beside the field, not in its label: the label stays its name alone. */}
      <span className="sb-note" id={`${hintId}-estimate`}>Free text, as the planner wrote it: a time or a size, such as &ldquo;about 2 hours&rdquo;. Optional.</span>
      <label className="rn-field">
        <b>Requirements, one per line</b>
        <textarea value={requirements} rows={4} onChange={(e) => setRequirements(e.target.value)} />
      </label>
      <p className="sb-note">
        The requirements are what the review checks, and what decides <code>Closes</code> against{' '}
        <code>part of</code>. The mode and the overlaps are kept as the planner found them.
      </p>
      {steps.map((s, i) => (
        <fieldset key={s.step.step_id} className="rn-field-set">
          <legend className="mono">{i + 1} · {s.step.step_id}</legend>
          <label className="rn-field">
            <b>Title</b>
            <input value={s.title} onChange={(e) => set(i, 'title', e.target.value)}
              aria-invalid={s.title.trim() === '' || undefined}
              aria-describedby={s.title.trim() === '' ? `${hintId}-t${i}` : undefined} />
          </label>
          {s.title.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-t${i}`}>Required: a step needs a title.</span>}
          <label className="rn-field">
            <b>Prompt</b>
            <textarea value={s.prompt} rows={4} onChange={(e) => set(i, 'prompt', e.target.value)}
              aria-invalid={s.prompt.trim() === '' || undefined}
              aria-describedby={s.prompt.trim() === '' ? `${hintId}-p${i}` : undefined} />
          </label>
          {s.prompt.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-p${i}`}>Required: a step needs a prompt.</span>}
          <label className="rn-field">
            <b>Files, one per line</b>
            <textarea className="mono" value={s.files} rows={2} onChange={(e) => set(i, 'files', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Tests, one per line</b>
            <textarea value={s.tests} rows={2} onChange={(e) => set(i, 'tests', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Step estimate</b>
            <input value={s.estimate} maxLength={100} onChange={(e) => set(i, 'estimate', e.target.value)} />
          </label>
        </fieldset>
      ))}
      <p className="sb-note">Saving sends the digest of the plan you opened; an edit made by someone else since is refused.</p>
      <div className="rn-actions">
        <Button type="submit" kind="primary" disabled={busy}
          disabledReason={blank ? `Not saved: ${blanks.join(', ')} ${blanks.length === 1 ? 'is' : 'are'} empty.` : undefined}>Save the plan</Button>
        <Button disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  )
}

/** A note's opening "No action", with the punctuation after it: cut from what is shown. */
const NO_ACTION_LEAD = /^\s*no action\b\s*(?:[:.;,]|--?|—|–)?\s*/i
/** "No action" opening the note, or a sentence of it: after `.`, `!` or `?` (and a closing quote or bracket), or a line break. */
const NO_ACTION_SENTENCE = /(?:^|[.!?]['"’”)\]]*\s+|\n\s*)no action\b/i

/**
 * WHAT THE PLANNER DECIDED ABOUT ONE OVERLAP. Its `action` field says so
 * (#587, owner decision 2026-10-05: a field, not better guessing): `none` or
 * `required`. A plan stored before the field has only the note, and the
 * planner said "No action" in it when it did nothing -- but not always first:
 * run_7a37942a19aa4d2c80d1's five notes each said what overlapped and THEN
 * "No action", and reading only the opening drew all five amber. So a legacy
 * note counts as no action when a sentence of it opens with those words;
 * anything else needs action, because an unread verdict is never "no action".
 *
 * "Not in this run", in neutral grey with the note under it (owner wording,
 * 2026-10-05): "No action" beside an amber card read as a dismissal of work
 * someone may still have to do elsewhere.
 *
 * A PLAN STORED BEFORE THE FIELD, ON A RUN THAT HAS ENDED (owner decision,
 * 2026-10-05): every such overlap is "Not in this run". A note written as an
 * instruction for while the run went cannot need action once the run is DONE,
 * FAILED, REJECTED or CANCELLED. An overlap WITH an `action` shows exactly its
 * field, ended run or not.
 */
export function overlapVerdict(
  o: Pick<PlanOverlap, 'note' | 'action'>,
  runEnded = false,
): { needs: boolean; word: string; why: string } {
  const note = typeof o?.note === 'string' ? o.note : ''
  const none = { needs: false, word: 'Not in this run' }
  const needs = { needs: true, word: 'Needs action' }
  if (o?.action === 'none') return { ...none, why: note }
  if (o?.action === 'required') return { ...needs, why: note }
  if (runEnded) return { ...none, why: note }
  if (!NO_ACTION_SENTENCE.test(note)) return { ...needs, why: note }
  const lead = NO_ACTION_LEAD.exec(note)
  return { ...none, why: (lead === null ? note : note.slice(lead[0].length)) || note }
}

/**
 * ONE OVERLAP AS SERVED, read as `PlanOverlap`. A plan stored before overlaps
 * were objects carries each as a bare sentence; the planner wrote it either
 * way, so it is drawn as its note with no ref, never dropped and never allowed
 * to throw while the page renders.
 */
function overlapRead(o: unknown): PlanOverlap {
  if (typeof o === 'string') return { ref: '', kind: 'issue', note: o }
  const r = (o ?? {}) as Partial<Record<keyof PlanOverlap, unknown>>
  return {
    ref: typeof r.ref === 'string' ? r.ref : '',
    kind: r.kind === 'pull_request' ? 'pull_request' : 'issue',
    ...(r.action === 'none' || r.action === 'required' ? { action: r.action } : {}),
    note: typeof r.note === 'string' ? r.note : '',
  }
}

/**
 * THE OVERLAPS THE PLANNER FOUND, above the plan. Each is a link to the issue
 * or pull request in flight, with its kind and the planner's verdict, and
 * opens to the planner's reasoning. A plan that has no `overlaps` field was
 * written before the planner read open work, and says so; an empty list is a
 * finding, and names what was read.
 *
 * AMBER ONLY WHEN ONE NEEDS ACTION (owner QA E, 2026-10-04): five long
 * paragraphs that each said "no action" sat in a warn-edged card that read as
 * a warning. The card is neutral until an overlap needs action, and its
 * heading counts both from `overlapVerdict`: "5 checked · none need action",
 * or "5 checked · 1 needs action".
 */
function Overlaps({ plan, openWork, folded, ended }: { plan: RunPlan; openWork: OpenWork | null; folded: boolean; ended: boolean }) {
  const overlaps = plan.overlaps === undefined || plan.overlaps === null ? plan.overlaps : plan.overlaps.map(overlapRead)
  const found = overlaps !== undefined && overlaps !== null && overlaps.length > 0
  const verdicts = found ? overlaps!.map((o) => overlapVerdict(o, ended)) : []
  const needing = verdicts.filter((v) => v.needs).length
  const title = found
    ? `Overlaps the planner found · ${overlaps!.length} checked · ${
      needing === 0 ? 'none need' : `${needing} ${needing === 1 ? 'needs' : 'need'}`} action`
    : 'Overlaps the planner found'
  const body = (
    <>
      {overlaps === undefined || overlaps === null ? (
        <p className="sb-note">
          This plan does not say: it was written without the planner&rsquo;s read of the repository&rsquo;s open issues
          and pull requests, so nothing here rules overlaps out.
        </p>
      ) : overlaps.length === 0 ? (
        <p className="sb-note">
          None found{openWork === null ? '.' : <>
            {' '}in {pluralise(openWork.issues.length, 'open issue')} and{' '}
            {pluralise(openWork.pull_requests.length, 'open pull request')} read when the run was created
            {openWork.issues_truncated || openWork.pull_requests_truncated ? ' (the list was cut, so not every one was read)' : ''}.
          </>}
        </p>
      ) : (
        <ul className="rn-overlap-list">
          {overlaps.map((o, i) => {
            const url = overlapUrl(o)
            const v = verdicts[i]!
            return (
              <li key={`${o.ref}-${i}`}>
                <details className={v.needs ? 'rn-overlap is-needs' : 'rn-overlap'}>
                  <summary>
                    {o.ref === ''
                      ? null
                      : url === null
                      ? <span className="mono">{o.ref}</span>
                      : <a href={url} target="_blank" rel="noreferrer" className="mono">{o.ref}</a>}
                    {o.ref === '' ? null : <Chip>{o.kind === 'pull_request' ? 'pull request' : 'issue'}</Chip>}
                    <span className="rn-overlap-v" title={o.action === undefined ? "Read from the planner's note" : "The planner's verdict"}>{v.word}</span>
                  </summary>
                  <p className="rn-overlap-note"><RnText text={v.why} /></p>
                </details>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
  const className = `rn-overlaps${needing > 0 ? ' is-found' : ''}`
  // ONE LINE ONCE THE PLAN IS APPROVED (lane U14 item 5): it was read before
  // approval; after it, it sat ~1,000px above nothing a reader needed.
  if (folded) {
    return (
      <Card level={3} className={className}>
        <Fold folded head={<h3>{title}</h3>} line={null}>{body}</Fold>
      </Card>
    )
  }
  return <Card level={3} className={className} title={title}>{body}</Card>
}

/**
 * A CARD FOLDED TO ONE LINE (lane U14 item 5): its heading and a short line
 * as a disclosure, the body under it. Unfolded, the heading and body as they
 * were.
 */
function Fold({ folded, head, line, children }: { folded: boolean; head: ReactNode; line: ReactNode; children: ReactNode }) {
  if (!folded) return <>{head}{children}</>
  return (
    <details className="rn-fold">
      <summary>{head}{line}</summary>
      <div className="rn-fold-b">{children}</div>
    </details>
  )
}

/**
 * THE FOLDED PLAN'S COUNT, IN THE STEPS CARD'S TERMS (QA G2-30, 2026-10-07):
 * "1 step · approved" sat beside "Steps · 3", because every compiled workflow
 * adds a review and a fix gated on its verdict (`plan_shape`, issueruns.py).
 */
function plannedSteps(plan: RunPlan): string {
  return `${pluralise(plan.steps.length, 'planned step')} + review + fix`
}

/**
 * THE REVISION A PLAN EDIT MADE (live QA RT-17, 2026-10-11): an edit is the
 * run's PLANNED -> PLANNED transition, and it read `Planned · from planned`.
 * The plan starts at revision 1, and each edit before this one, and this one,
 * adds one.
 */
export function planRevisionAt(entries: readonly ProgressEntry[], i: number): number {
  let n = 1
  for (let k = 0; k <= i && k < entries.length; k++) {
    const e = entries[k]!
    if (e.kind === 'run' && e.from === 'PLANNED' && e.to === 'PLANNED') n += 1
  }
  return n
}

/** Whether the plan has been approved: it is no longer the page's question. */
function planApproved(run: IssueRun): boolean {
  return run.approved_by !== null || run.workflow_id !== null
}

/** The issue's requirements as the planner listed them; one the review left open is marked. */
function Requirements({ plan, unmet }: { plan: RunPlan; unmet: string[] }) {
  const reqs = plan.requirements
  if (reqs === undefined || reqs === null || reqs.length === 0) {
    // `part of #N` QUOTED (runs-submit-28): "the pull request says part of." read as a broken sentence.
    return <p className="sb-note">The plan lists no requirements, so a review cannot confirm every one: the pull request says <code>part of #N</code>, not <code>Closes</code>.</p>
  }
  const open = new Set(unmet)
  return (
    <div className="rn-reqs-wrap">
      <b className="rn-sub">Requirements · {reqs.length}</b>
      <ol className="rn-reqs">
        {reqs.map((r, i) => (
          <li key={`${i}-${r}`} className={open.has(r) ? 'is-unmet' : undefined}>
            <RnText text={r} />{open.has(r) && <i className="sbf-bad"> · not confirmed by the review</i>}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** A comment the write-back posted on the issue, or why there is none. */
function CommentFact({ label, name, url, absent }: { label: string; name: string; url: string | null; absent: string }) {
  return (
    <li className={url === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
      <b>{label}</b>
      {url === null ? <i className="ctl-em" title={absent}>{absent}</i> : <a href={url} target="_blank" rel="noreferrer" title={url}>{name}</a>}
    </li>
  )
}

/**
 * THE PULL REQUEST THE RUN'S WORKFLOW OPENED, READ FROM ITS INTEGRATOR STEP
 * (browser QA N15, 2026-10-04). A run created before the CI loop records no
 * `pull_request` of its own, and its page said "none yet · the workflow opens
 * it" under a Done run whose workflow had opened PR #545. When the run has
 * none, the workflow is read once and its integrator's
 * `result_summary.git.pull_request` is the answer (`workflowPullRequest`, the
 * same reading the Workflows screen draws). Null until read, or when the run
 * names its own PR or no workflow.
 */
/**
 * What the workflow read said about its pull request. `none` when nothing was
 * asked (the run names its own PR, or it has no workflow); `reading` and
 * `error` are unknowns, and an unknown is never drawn as "none".
 */
type WfPrRead =
  | { kind: 'none' }
  | { kind: 'reading' }
  | { kind: 'error'; why: string }
  | { kind: 'read'; pr: WorkflowPullRequest | null }

/**
 * The PR the integrator opened, from the run's workflow read (`useRunWorkflows`,
 * which the Steps card shares), asked only when the run names no PR itself.
 */
function wfPrOf(run: IssueRun, read: RunWorkflows): WfPrRead {
  if ((run.pull_request ?? null) !== null || run.workflow_id === null) return { kind: 'none' }
  const main = read.loads?.[0]
  if (main === undefined || main.wf !== run.workflow_id) return { kind: 'reading' }
  if (main.data === null) return { kind: 'error', why: main.error ?? 'the read did not finish' }
  const pr = workflowPullRequest(main.data.workflow, new Map(main.data.tasks.map((t) => [t.id, t])))
  return { kind: 'read', pr }
}

/** The PR the workflow's integrator opened, once read; null otherwise. */
function prOf(read: WfPrRead): WorkflowPullRequest | null {
  return read.kind === 'read' ? read.pr : null
}

/** The run's pull request: a link once the workflow opened one. */
function PullRequestFact({ run, wfPr: read }: { run: IssueRun; wfPr: WfPrRead }) {
  const pr = run.pull_request ?? null
  const url = githubLink(pr?.url)
  const label = pr?.number === null || pr?.number === undefined ? 'pull request' : `#${pr.number}`
  const wfPr = prOf(read)
  if (pr === null && wfPr !== null) {
    const title = `#${wfPr.number}, read from the workflow's integrator result (step ${wfPr.stepId}); the run itself records no pull request.`
    return (
      <li className="ctl-fact">
        <b>Pull request</b>
        {wfPr.href === null
          ? <span className="mono" title={title}>#{wfPr.number}</span>
          : <a href={wfPr.href} target="_blank" rel="noreferrer" className="mono" title={title}>#{wfPr.number}</a>}
      </li>
    )
  }
  // AN UNREAD WORKFLOW IS AN UNKNOWN, NOT "NONE" (review of U11b N15): while
  // the workflow is being read, or after its read failed, whether it opened a
  // pull request is not known.
  if (pr === null && (read.kind === 'reading' || read.kind === 'error')) {
    const why = read.kind === 'reading'
      ? 'The run records no pull request; its workflow is still being read for the one its integrator opened.'
      : `The run records no pull request, and its workflow could not be read for the one its integrator opened: ${read.why}`
    // THE REASON IN WORDS, AS EVERY OTHER ABSENT FACT HERE (QA1-FIX-24,
    // runs-submit-31, part of #2596): a bare dash sat where the Planned run
    // says "none yet · the workflow opens it"; its why was only a title. The
    // dash stays (an unknown is a dash, U11b N15), its reason beside it.
    return (
      <li className="ctl-fact is-absent">
        <b>Pull request</b>
        <Dash why={why} />{' '}
        <i className="ctl-em" title={why} data-pr-unknown="">
          {read.kind === 'reading' ? 'not known yet · reading its workflow' : 'not known · its workflow was not read'}
        </i>
      </li>
    )
  }
  const none = run.terminal ? 'none recorded' : 'none yet · the workflow opens it'
  return (
    <li className={pr === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
      <b>Pull request</b>
      {pr === null
        ? <i className="ctl-em" title={run.terminal ? (run.workflow_id === null ? 'The run started no workflow, so no pull request was opened.' : 'Neither the run nor its workflow recorded a pull request.') : 'None yet: the workflow opens it.'}>{none}</i>
        : url === null ? <span className="mono" title={label}>{label}</span>
          : <a href={url} target="_blank" rel="noreferrer" className="mono" title={url}>{label}</a>}
    </li>
  )
}

/**
 * HOW LONG A CHECKING RUN MAY GO WITH NO CI READ BEFORE THE CARD SAYS SO
 * (live QA RT-4, 2026-10-11): a run sat in CHECKING for 15 hours with
 * `checks: null` and the card said `not read yet`, with no age and no
 * warning. The CI loop reads a pull request within minutes of it opening; 30
 * minutes without one is a loop that is not reading it, not one that is late.
 */
export const CI_UNREAD_WARN_MS = 30 * 60_000

/** When the run last entered CHECKING, from its history; null when it never did or recorded no time. */
export function checkingSince(run: IssueRun): number | null {
  for (let i = run.history.length - 1; i >= 0; i--) {
    const h = run.history[i]!
    if (h.to === 'CHECKING') {
      const t = h.at === null ? NaN : Date.parse(h.at)
      return Number.isFinite(t) ? t : null
    }
  }
  return null
}

/**
 * THE CHECKS FACT WITH NO READING. A finished run's is final (live QA RT-8:
 * "not read yet" on a DONE run with no pull request read as a read still to
 * come); a CHECKING run past CI_UNREAD_WARN_MS warns, with the time it began.
 */
function ChecksUnread({ run, now }: { run: IssueRun; now: number }) {
  const pr = run.pull_request ?? null
  if (run.terminal) return <i className="ctl-em">{pr === null ? 'none · no pull request' : 'never read'}</i>
  const since = run.state === 'CHECKING' ? checkingSince(run) : null
  if (since !== null && now - since > CI_UNREAD_WARN_MS) {
    const at = new Date(since).toISOString()
    return (
      <span className="rn-ci-unread" role="status" data-warn=""
        title="The CI loop has recorded no reading of this pull request's checks. It reads them within minutes of the run checking; this long without one, it is not reading them.">
        <WarnMark /> no CI read · the run began checking <time dateTime={at}>{timeAgo(at, now)}</time>
      </span>
    )
  }
  return <i className="ctl-em">not read yet</i>
}

/**
 * A LOG TAIL WITHOUT ITS TERMINAL COLOURS (live QA RT-14, 2026-10-11): the
 * excerpt is a CI log's tail, and it showed `[36;1mfailed=… [0m`. SGR
 * sequences -- ESC `[` digits and `;` then `m` -- are dropped, with or
 * without the ESC (the server's redaction can leave the bracket alone).
 */
export function stripSgr(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b?\[\d{1,3}(?:;\d{1,3})*m|\u001b\[m/g, '')
}

/** Whether the run has reached the pull request and its CI, so the CI card has something to say. */
function hasCi(run: IssueRun): boolean {
  return (run.pull_request ?? null) !== null || run.state === 'CHECKING' || run.state === 'FIXING'
    || (run.ci_fix_round ?? 0) > 0 || Boolean(run.green_sha) || Boolean(run.failure_excerpt)
    || (run.requirements_met ?? null) !== null
}

/**
 * THE PULL REQUEST AND ITS CI (the CI loop, `issueci`): the checks at the
 * head, the fix round of the cap, the green sha a DONE run is pinned to, the
 * keyword the requirements finding chose, and a FAILED run's excerpt -- the
 * server's redacted text, drawn as text.
 */
function CiCard({ run, go, wfPr: read, index, merge }: {
  run: IssueRun; go: (to: string) => void; wfPr: WfPrRead; index: RunIndexRead; merge: MergeCard | null
}) {
  const now = useNow()
  if (!hasCi(run)) {
    // A FINISHED RUN DRAWS THE CARD ANYWAY (browser QA N15): no card at all
    // read as "nothing to say", on a run whose workflow opened a PR. The CI
    // loop records checks on the run; a run it never read says so. "Created
    // before the CI loop" is said ONLY where a pull request exists to have
    // been read (review of U11b N15): a rejected run, a failed planner or a
    // run cancelled before its workflow opened nothing for CI to check.
    if (!run.terminal) return null
    const wfPr = prOf(read)
    let note: string | null
    if (wfPr !== null) {
      note = `No CI recorded for this run: the CI loop never read its pull request (#${wfPr.number}, opened by the workflow), which is how a run created before the CI loop ends.`
    } else if (read.kind === 'reading') {
      note = null
    } else if (read.kind === 'error') {
      note = `No CI recorded for this run, and its workflow could not be read to say whether it opened a pull request: ${read.why}`
    } else if (run.workflow_id === null) {
      const why = run.state === 'REJECTED' ? 'the plan was rejected'
        : run.state === 'CANCELLED' ? 'the run was cancelled'
          : run.state === 'FAILED' ? 'the run failed'
            : 'the run ended'
      note = `No pull request was opened, so there is no CI to read: ${why} before any workflow started.`
    } else {
      note = 'No pull request was opened: the workflow ended without one, so there is no CI to read.'
    }
    if (note === null) return null
    return (
      <Card level={3} className="rn-ci" title="Pull request and checks">
        <p className="sb-note">{note}</p>
      </Card>
    )
  }
  const pr = run.pull_request ?? null
  const round = run.ci_fix_round ?? 0
  const fixes = run.ci_fix_workflows ?? []
  const n = run.issue.number
  const prUrl = githubLink(pr?.url)
  const head = pr?.head_sha ?? null
  // THE FAILED CHECKS BY NAME (lane U14 item 5): the run serves no per-check
  // list, but its failure excerpt names them, at the sha it read them at. A
  // reading from before the head moved is said as that, never as the head's.
  const red = redReading(run.failure_excerpt)
  const redAtHead = red !== null && head !== null && head.startsWith(red.sha)
  const notServed = (what: string) =>
    `${what} not served: the run records one aggregate reading of the required checks (pull_request.checks), not each check.`
  // THE PENDING CHECKS BY NAME (#503): a merge step parked on CI_PENDING
  // records the checks it waits for (`metadata.merge_wait.pending`) at the
  // head it read them at -- the one served list of pending checks.
  const waiting = merge?.state.kind === 'waiting' && merge.state.pending.length > 0 ? merge.state : null
  // THE SERVED COUNTS AND LIST (#503): swarm-api records them on every CI
  // read since this change; an older run serves null and the card falls
  // back to what the excerpt and the merge step name.
  const served = pr?.check_counts ?? null
  const checkList = pr?.check_list ?? null
  const readError = pr?.read_error?.trim() ? pr.read_error.trim() : null
  // THE FAILING CHECK'S LINK FIRST (live QA RT-14): `ci_url` led to the
  // first job (`secret scan`), not to the `ci-gate` that failed. A failed
  // check in the served list wins; `ci_url` is the fallback.
  const ciUrl = checkLink(checkList?.find((c) => c.state === 'failed' && checkLink(c.url) !== null)?.url ?? pr?.ci_url)
  const counts: string[] = []
  if (redAtHead && pr?.checks === 'red') counts.push(`failed ${red!.names.length}`)
  if (waiting !== null) counts.push(`pending ${waiting.pending.length}`)
  const known = mergeKnown(run, merge)
  const issueUrl = githubLink(run.issue.url)
  return (
    <Card level={3} className={lead(run) ? 'rn-ci is-lead' : 'rn-ci'} title={pr === null || pr.number === null ? 'Pull request and checks' : (
      <>
        Pull request{' '}
        {prUrl === null ? <span className="mono">#{pr.number}</span>
          : <a href={prUrl} target="_blank" rel="noreferrer" className="mono" title={prUrl}>#{pr.number}</a>}
      </>
    )}>
      <ul className="ctl-facts rn-ci-facts">
        <li className="ctl-fact">
          <b>checks</b>
          {pr?.checks
            ? <span>{run.terminal ? 'last read before the run ended: ' : ''}{pr.checks}{head ? <> at <code title={head}>{shortSha(head)}</code><CopyText text={head} /></> : null}</span>
            : readError !== null
            ? <i className="ctl-em" data-ci-read="failed">not read</i>
            : <ChecksUnread run={run} now={now} />}
        </li>
        {/* WHY THERE IS NO READING (RT-4, 2026-10-11): a run whose every CI
            read failed -- a 403 on commit statuses -- said "not read yet" for
            15 hours. The server's `read_error` is the reason, masked and
            bounded there; `checked_at` is when it was last tried. */}
        {readError !== null && (
          <li className="ctl-fact rn-ci-error" role="status">
            <b>last read</b>
            <span>
              <WarnMark /> {readError}
              {pr?.checked_at ? <> · <span title={pr.checked_at}>{timeAgo(pr.checked_at)}</span></> : null}
            </span>
          </li>
        )}
        {/* ONE REASON, NOT FOUR DASHES (QA G2-06, 2026-10-07): "passed — ·
            pending — · failed — · skipped —" beside "checks green" read as a
            result every count disowned. The one count the run can carry is
            the failures its excerpt names at this head. */}
        <li className="ctl-fact rn-ci-counts">
          <b>by check</b>
          {served !== null
            ? <span title="The last CI read's count per check: neutral, skipped and cancelled runs count as skipped.">{checkCountsText(served)}</span>
            : counts.length > 0
            ? <span>{counts.join(' · ')} · <i className="ctl-em" title={notServed(waiting !== null ? 'How many passed or were skipped is' : 'How many passed, are pending or were skipped is')}>the other counts not served</i></span>
            : <i className="ctl-em" title={notServed('Each count is')}>per-check counts not served</i>}
        </li>
        {checkList !== null && checkList.length > 0 && (
          <li className="ctl-fact rn-ci-list">
            <b>each check</b>
            <details className="rn-check-list">
              <summary>
                {pluralise(checkList.length, 'check')}
                {pr?.check_list_truncated === true ? ' shown · the rest not kept' : ''}
              </summary>
              <ul className="rn-failed-checks">
                {checkList.map((c, i) => {
                  const href = checkLink(c.url)
                  return (
                    <li key={`${i}-${c.name}`} data-state={c.state ?? undefined}>
                      {href === null
                        ? <span title="GitHub gave this check no link.">{c.name}</span>
                        : <a href={href} target="_blank" rel="noreferrer" title={href}>{c.name}</a>}
                      {c.state !== null && <> · <span className="ctl-em">{c.state}</span></>}
                    </li>
                  )
                })}
              </ul>
            </details>
          </li>
        )}
        {waiting !== null && (
          <li className="ctl-fact rn-ci-pending">
            <b>{waiting.head === null ? 'pending' : <>pending at <code title={waiting.head}>{shortSha(waiting.head)}</code></>}</b>
            <ul className="rn-failed-checks">
              {waiting.pending.map((name, i) => (
                <li key={`${i}-${name}`}>
                  <span className="rn-pending-check" title="The merge step's last read of the checks it waits for (merge_wait.pending).">{name}</span>
                </li>
              ))}
            </ul>
          </li>
        )}
        {red !== null && red.names.length > 0 && (
          <li className="ctl-fact rn-ci-failed">
            <b>{redAtHead ? 'failed' : `red at ${red.sha}, before the head moved`}</b>
            <ul className="rn-failed-checks">
              {red.names.map((name, i) => (
                <li key={`${i}-${name}`}>
                  <span className="rn-failed-check" title="Each check's own run link is not served; the checks page lists them.">{name}</span>
                </li>
              ))}
            </ul>
          </li>
        )}
        {prUrl !== null && (
          <li className="ctl-fact">
            <b>CI</b>
            <a href={`${prUrl}/checks`} target="_blank" rel="noreferrer">checks on GitHub ↗</a>
            {ciUrl !== null && <>{' · '}<a href={ciUrl} target="_blank" rel="noreferrer" title={ciUrl}>CI run →</a></>}
          </li>
        )}
        {prUrl === null && ciUrl !== null && (
          <li className="ctl-fact">
            <b>CI</b>
            <a href={ciUrl} target="_blank" rel="noreferrer" title={ciUrl}>CI run →</a>
          </li>
        )}
        <li className="ctl-fact">
          <b>fix rounds</b>
          <span>
            {round === 0 ? `none spent · up to ${run.fix_rounds}` : `fix round ${round} of ${run.fix_rounds}`}
            {fixes.map((wf) => (
              <span key={wf}>
                {' · '}
                <InApp go={go} to={`work/workflows?${new URLSearchParams({ wf }).toString()}`}
                  href={`/workflows/${encodeURIComponent(wf)}`}>{wf}</InApp>
              </span>
            ))}
          </span>
        </li>
        <li className={run.green_sha ? 'ctl-fact' : 'ctl-fact is-absent'}>
          <b>green at</b>
          {/* SHORT, LIKE CHECKS (item 6): the whole sha is its title and its copy. */}
          {run.green_sha
            ? <span><code title={run.green_sha}>{shortSha(run.green_sha)}</code><CopyText text={run.green_sha} /></span>
            : <i className="ctl-em">{!run.terminal ? 'not green yet' : pr === null ? 'none · no pull request' : 'never green'}</i>}
        </li>
        {pr !== null && <MergeFact run={run} known={known} />}
        <IssueFact run={run} known={known} url={issueUrl} />
        <li className="ctl-fact">
          <b>keyword</b>
          {run.requirements_met === true ? <code>Closes #{n}</code>
            : run.requirements_met === false ? <code>part of #{n}</code>
              : <i className="ctl-em">not decided · the review has not reported on the requirements</i>}
        </li>
      </ul>
      <SelectedTestsGate ctx={index} go={go} />
      {run.requirements_met === false && (
        <p className="sb-note">
          The review left {pluralise((run.requirements_unmet ?? []).length, 'requirement')} open
          {run.requirements_note ? <>: {run.requirements_note}</> : '.'}
        </p>
      )}
      {run.failure_excerpt && (
        <div className="rn-excerpt">
          <CodeBlock title="Failing checks" lang="redacted by the API" text={stripSgr(run.failure_excerpt)} />
        </div>
      )}
    </Card>
  )
}

/** Whether the pull request is the run's subject now: CHECKING, FIXING or DONE. */
function lead(run: IssueRun): boolean {
  return run.state === 'CHECKING' || run.state === 'FIXING' || run.state === 'DONE'
}

/** The run's merge step's card, the newest round's when several have one; null when no step is a merge step. */
export function runMergeCard(rows: readonly StepRow[]): MergeCard | null {
  for (let i = rows.length - 1; i >= 0; i--) {
    const card = rows[i]?.merge ?? null
    if (card !== null) return card
  }
  return null
}

/** What the served data settles about the pull request's merge. */
export type MergeKnown =
  /** `step`: the merge step's result says so; `ci`: the CI loop's DONE without a green sha does. */
  | { kind: 'merged'; via: 'step'; commit: string | null; already: boolean; closed: readonly number[]; at: string | null }
  | { kind: 'merged'; via: 'ci'; at: string | null }
  /** A finished run whose last CI read served `pull_request.merged: false`. */
  | { kind: 'not-merged' }
  | { kind: 'refused'; failed: boolean; code: string | null; reason: string | null }
  /** The merge step is parked on CI_PENDING: open, and it merges once the checks settle. */
  | { kind: 'waiting' }
  /** CHECKING or FIXING: the last CI read found it open. */
  | { kind: 'open' }
  | { kind: 'unknown' }

/**
 * WHETHER THE PULL REQUEST MERGED (lane U14 item 6, #503). The CI loop reads
 * it, and since #503's API half `IssueRun.to_api` serves it with its time
 * (`pull_request.merged`, `merged_at`): true is merged, even with no merge
 * step, and false on a finished run is not merged. Null -- a run never read,
 * or one stored before the field -- falls back to two served things:
 *
 *  1. the merge step's own result (`result_summary.merge`, read by
 *     `mergeCardOf` from the run's workflow): merged, with its commit and the
 *     issues it closed, or refused, or parked waiting for CI;
 *  2. the CI loop's transitions (`issueci`): a closed pull request FAILS the
 *     run, and a green one that ends it DONE always records `green_sha`. So a
 *     DONE run with a pull request and no green sha was ended by GitHub
 *     reporting it merged.
 *
 * A DONE run that IS green settles nothing: green alone ends a run without
 * auto_merge, and a merge at green records the same sha. That case still
 * says "merge not reported" and names the field. While CHECKING or FIXING the
 * last read found it open, since a merged PR ends the run and a closed one
 * fails it.
 */
export function mergeKnown(run: IssueRun, card: MergeCard | null): MergeKnown {
  const s = card?.state
  const pr = run.pull_request ?? null
  const at = pr?.merged === true ? (pr.merged_at ?? null) : null
  if (s?.kind === 'merged') return { kind: 'merged', via: 'step', commit: s.commit, already: s.already && s.byThisTask !== true, closed: s.closed, at }
  // GitHub's own answer, read by the CI loop, outranks a step's refusal: a
  // pull request someone merged by hand after the step refused is merged.
  if (pr?.merged === true) return { kind: 'merged', via: 'ci', at }
  if (s?.kind === 'refused') return { kind: 'refused', failed: s.failed, code: s.code, reason: s.reason }
  if (s?.kind === 'waiting' || s?.kind === 'updated') return { kind: 'waiting' }
  if (run.state === 'CHECKING' || run.state === 'FIXING') return { kind: 'open' }
  if (run.terminal && pr?.merged === false) return { kind: 'not-merged' }
  if (run.state === 'DONE' && pr !== null && !run.green_sha && runOutcomeLabel(run) === null) {
    return { kind: 'merged', via: 'ci', at: null }
  }
  return { kind: 'unknown' }
}

function MergeFact({ run, known }: { run: IssueRun; known: MergeKnown }) {
  const now = useNow()
  const why = 'Not reported: this run serves no pull_request.merged -- the CI loop never read it, or the run was stored before the field was recorded.'
  const when = known.kind === 'merged' && known.at !== null
    ? <> <span title={utcTitle(known.at)}>{timeAgo(known.at, now)}</span></>
    : null
  switch (known.kind) {
    case 'merged':
      return (
        <li className="ctl-fact rn-ci-merge" data-merge="merged">
          <b>merge</b>
          {known.via === 'step'
            ? (
              <span title="From the merge step's result (result_summary.merge).">
                merged{when}
                {known.commit !== null && <> · <code title={known.commit}>{shortSha(known.commit)}</code><CopyText text={known.commit} /></>}
                {known.already ? ' · already merged at the pinned head' : ' · by the merge step'}
              </span>
            )
            : known.at !== null
              ? <span title="GitHub reported it merged at the CI loop's last read (pull_request.merged, merged_at).">merged{when}</span>
              : (
                <span title="GitHub reported it merged (pull_request.merged), or -- on a run stored before that was served -- a DONE run with no green sha was ended by it: a closed pull request fails the run and a green one records its sha. The merge commit and time are not served.">
                  merged · <i className="ctl-em">commit and time not served</i>
                </span>
              )}
        </li>
      )
    case 'not-merged':
      return (
        <li className="ctl-fact rn-ci-merge" data-merge="not-merged">
          <b>merge</b>
          <span title="The CI loop's last read found the pull request not merged (pull_request.merged).">not merged</span>
        </li>
      )
    case 'refused':
      return (
        <li className="ctl-fact rn-ci-merge" data-merge="refused">
          <b>merge</b>
          <span title={known.reason ?? undefined}>
            not merged · {known.failed ? 'the merge step failed' : 'the merge step refused'}
            {known.code !== null && <> · <code>{known.code}</code></>}
          </span>
        </li>
      )
    case 'waiting':
      return (
        <li className="ctl-fact rn-ci-merge" data-merge="waiting">
          <b>merge</b>
          <span title="The merge step is parked on CI_PENDING: it holds no capacity and merges once the checks settle.">open · the merge step waits for CI</span>
        </li>
      )
    case 'open':
      return (
        <li className="ctl-fact is-absent">
          <b>merge</b>
          <i className="ctl-em" title="A merged pull request ends the run DONE and a closed one fails it, so a CHECKING or FIXING run's was open at its last read.">open · not merged at the last read</i>
        </li>
      )
    case 'unknown':
      return (
        <li className="ctl-fact is-absent">
          <b>merge</b>
          <i className="ctl-em" title={run.state === 'DONE' ? why : undefined}>merge not reported</i>
        </li>
      )
  }
}

/**
 * WHETHER THE ISSUE CLOSED (#503). The issue's state now is not served: the
 * run read the issue once, at submission (`issue_read`). What is served:
 * `issue_closed`, set when the run's own write-back closed it (an
 * already_on_main run with every requirement met); the merge step's
 * `issues_closed`; and, for a pull request the CI loop saw merged, the
 * keyword the run wrote into it -- `Closes #N` closes the issue on a merge
 * into the default branch, `part of #N` does not.
 */
function issueKnown(run: IssueRun, known: MergeKnown): { text: string; title: string; settled: boolean } {
  const n = run.issue.number
  if (run.issue_closed === true) {
    return { text: 'closed by this run', settled: true, title: "The run's write-back closed it (issue_closed): the work was already on main and every requirement was shown met." }
  }
  if (known.kind === 'merged' && known.via === 'step') {
    return known.closed.includes(n)
      ? { text: 'closed by the merge', settled: true, title: "The merge step's result lists it among the issues it closed (issues_closed)." }
      : { text: 'not closed by the merge', settled: true, title: "The merge step's result lists the issues it closed (issues_closed); this one is not among them. Its state now is not served." }
  }
  if (known.kind === 'merged' && run.requirements_met === true) {
    return { text: `closed by the merge · Closes #${n}`, settled: true, title: `The merged pull request carries "Closes #${n}", which closes the issue on a merge into the default branch. Its state now is not served.` }
  }
  if (known.kind === 'merged' && run.requirements_met === false) {
    return { text: `left open · part of #${n}`, settled: true, title: `The merged pull request says "part of #${n}", which does not close the issue. Its state now is not served.` }
  }
  return {
    text: `state not served${run.issue_read?.state ? ` · ${run.issue_read.state} when the run was created` : ''}`,
    settled: false,
    title: "The issue's state now is not served: the run read the issue once, at submission (issue_read).",
  }
}

function IssueFact({ run, known, url }: { run: IssueRun; known: MergeKnown; url: string | null }) {
  const n = run.issue.number
  const said = issueKnown(run, known)
  return (
    <li className="ctl-fact rn-ci-issue">
      <b>issue</b>
      <span>
        {url === null ? <span className="mono">#{n}</span>
          : <a href={url} target="_blank" rel="noreferrer" className="mono" title={url}>#{n}</a>}
        {' · '}
        {said.settled
          ? <span title={said.title}>{said.text}</span>
          : <i className="ctl-em" title={said.title}>{said.text}</i>}
      </span>
    </li>
  )
}

/**
 * THE FORM TAKES THE FOCUS IT OPENS (QA1-FIX-24, runs-submit-08, part of
 * #2596): Enter on Reject drew the form and left the focus on <body>, and
 * Escape did nothing. Its Why field has the focus once it opens, and Escape
 * anywhere in it is "Keep it" (which gives the focus back to Reject).
 */
function RejectForm({ busy, onCancel, onReject }: { busy: boolean; onCancel: () => void; onReject: (reason: string) => void }) {
  const [reason, setReason] = useState('')
  const why = useRef<HTMLTextAreaElement>(null)
  useEffect(() => why.current?.focus(), [])
  return (
    <form className="rn-reject" onSubmit={(e) => {
      e.preventDefault()
      onReject(reason)
    }} onKeyDown={(e) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      onCancel()
    }}>
      <label className="rn-field">
        <b>Why (optional)</b>
        <textarea ref={why} value={reason} rows={2} maxLength={1024} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="rn-actions">
        <Button type="submit" disabled={busy}>Reject this plan</Button>
        <Button disabled={busy} onClick={onCancel}>Keep it</Button>
      </div>
    </form>
  )
}
