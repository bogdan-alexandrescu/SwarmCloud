/**
 * THE PLAN AS A DOCUMENT (docs/design/plans-pipeline.md §2.3, lane U3
 * DECISION-DOCUMENT): the decision page of a `plan` or `hold` approval draws
 * the plan in the order a decision is made -- the summary, the requirements
 * (folded past the first), the steps (id, files, tests, estimate, done-when,
 * prompt) with each step's risk rows (§3.7), and the expected cost (§3.8) --
 * with a margin holding the planner's reason against each part (§3.6).
 *
 * THE BAR NAMES WHAT APPROVE APPROVES. `PlanBar` names the revision and the
 * digest; Approve (ApprovalDecision.tsx's `Decide`) sends exactly that digest.
 * When a poll of the run (`GET /v1/runs/{id}`) brings a newer digest while the
 * page shows an older one, the page keeps what it shows, takes Approve away and
 * offers one button to read the new revision (`D:P2c`): a poll never swaps the
 * plan under an open Approve. The server refuses a stale digest with 409
 * `plan_changed` anyway; this only says so before the click.
 *
 * EVERYTHING HERE IS READ, NEVER COMPUTED. The risk rows and the cost are the
 * platform's readings of the plan, served on the run read; the browser does
 * not restate the hold patterns or the price table (§3.7, §3.8). A run read
 * without them (an older server, `D:P6d`) draws the plan without them.
 *
 * PLAN TEXT IS DATA. Every field is drawn as a React text child, never as
 * HTML: a planner's summary holding markup shows that markup as characters.
 *
 * MOUNT POINTS for the lanes after this one (§11): `pinned` takes the
 * "Pinned to the plan" strip above the document (U8, PinnedDiagrams.tsx);
 * `thread(anchor)` takes each part's thread entries under its reason in the
 * margin (U4, PlanThread.tsx), keyed `plan`, `requirements` and
 * `step:<step_id>` as §3.4's anchors are. The review chat's button belongs to
 * the page head (U9), not to this component.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { loadRun } from './api'
import { Button, Dash } from './components'
import { errorHeading, type Result } from './fetch'
import { InfoTip } from './InfoTip'
import { timeAgo } from './Shell'
import { useNow } from './useNow'
import { PlanThread, openComments, useBarRegistration, usePlanConversation, ReplanControls } from './PlanThread'
import { PlanRevisionPicker } from './PlanRevisions'
import './styles/plan-document.css'

type Json = Record<string, unknown>

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.flatMap((x) => text(x) ?? []) : [])
const objs = (v: unknown): Json[] => (Array.isArray(v) ? v.filter((x): x is Json => x !== null && typeof x === 'object' && !Array.isArray(x)) : [])
const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** How often the page re-reads the run to learn of a newer revision. */
export const PLAN_POLL_MS = 20_000

/** The revision the page shows and the digest Approve sends for it. */
export interface ShownRevision {
  /** Null when the run read serves no revisions (an older server, `D:P6d`). */
  revision: number | null
  digest: string | null
}

/** A newer revision a poll found while an older one is on screen. */
export interface NewerRevision {
  revision: number | null
  digest: string
  changed: number
}

/**
 * The revision the page shows: the approval's digest is what the approve
 * route checks, so it is the digest named and sent; its number is the
 * revision of `plan_revisions` with that digest, else the run's own
 * `plan_revision` when the run's digest is the same one.
 */
export function shownRevision(digest: string | null, run: Json | null): ShownRevision {
  if (run === null) return { revision: null, digest }
  const served = objs(run.plan_revisions)
  const named = digest === null ? null : (served.find((r) => r.digest === digest) ?? null)
  if (named !== null) return { revision: num(named.revision), digest }
  if (served.length > 0 && digest !== null && run.plan_digest === digest) return { revision: num(run.plan_revision), digest }
  return { revision: null, digest }
}

/** The newest revision a run read names, when its digest is not the one shown. */
export function newerThan(shown: string | null, run: Json): NewerRevision | null {
  const digest = text(run.plan_digest)
  if (digest === null || shown === null || digest === shown) return null
  const top = objs(run.plan_revisions)[0] ?? null
  const same = top !== null && top.digest === digest
  return { revision: same ? num(top.revision) : num(run.plan_revision), digest, changed: same && Array.isArray(top.changed) ? top.changed.length : 0 }
}

/**
 * Re-reads the run every `everyMs` and returns the newer revision once one
 * arrives. A read that fails changes nothing: the shown plan stays, and the
 * server still refuses a stale Approve.
 */
export function useNewerRevision(runId: string | null, digest: string | null, everyMs: number = PLAN_POLL_MS): NewerRevision | null {
  const [newer, setNewer] = useState<NewerRevision | null>(null)
  useEffect(() => {
    if (runId === null || digest === null) return
    let live = true
    const tick = async () => {
      try {
        const r = await loadRun(runId)
        if (!live || (r.status !== 'ok' && r.status !== 'stale')) return
        const found = newerThan(digest, r.data.run as unknown as Json)
        if (found !== null) setNewer(found)
      } catch {
        // A failed poll is not news: the shown revision stays.
      }
    }
    const id = setInterval(() => void tick(), everyMs)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [runId, digest, everyMs])
  return newer
}

const shortDigest = (d: string) => {
  const hex = d.replace(/^sha256:/, '')
  return hex.length > 12 ? hex.slice(0, 12) : hex
}

/** The revision and the digest, as the bar names them. */
function Names({ shown }: { shown: ShownRevision }) {
  return (
    <span className="pd-names" data-testid="pd-names">
      {shown.revision !== null && (
        <b className="pd-rev" data-testid="pd-revision">
          Revision {shown.revision}
        </b>
      )}
      {shown.digest === null ? (
        <Dash why="no digest served" />
      ) : (
        <code className="pd-digest" title={shown.digest} data-testid="pd-digest" data-digest={shown.digest}>
          {shortDigest(shown.digest)}
        </code>
      )}
    </span>
  )
}

/**
 * THE DECISION BAR'S NAMING LINE (`D:P2a`): the revision and the digest that
 * the verbs in `children` act on. Superseded (`D:P2c`), it holds no verb: it
 * names the newer revision and offers one button to read it.
 */
export function PlanBar({ shown, newer, onRead, children }: { shown: ShownRevision; newer: NewerRevision | null; onRead: () => void; children: ReactNode }) {
  if (newer !== null) {
    const next = newer.revision === null ? 'the new revision' : `revision ${newer.revision}`
    return (
      <div className="pd-bar is-newer" role="group" aria-label="Decision" data-testid="pd-bar" data-superseded="true">
        <span className="pd-bar-l">
          <Names shown={shown} />
          <span className="pd-arrived" data-testid="pd-arrived">
            {newer.revision === null ? 'New revision' : `Revision ${newer.revision}`}
            {newer.changed > 0 && <span className="pd-dim"> · {newer.changed} changed</span>}
          </span>
          <InfoTip about="the new revision" tip={`The plan changed while this one was open. Approve is off for the revision shown; read ${next} to decide on it.`} />
        </span>
        <Button kind="primary" onClick={onRead}>
          {`Read ${next}`}
        </Button>
      </div>
    )
  }
  return (
    <div className="pd-bar" role="group" aria-label="Decision" data-testid="pd-bar">
      <span className="pd-bar-l">
        <Names shown={shown} />
        <InfoTip about="what Approve approves" tip="Approve approves this revision of the plan, by its digest. A plan changed since it was read is refused, never approved unread." />
      </span>
      {/* Request changes, or what Approve does to a running re-plan (PlanThread.tsx, §5.3). */}
      <ReplanControls digest={shown.digest} />
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The document
// ---------------------------------------------------------------------------

/** Each hold category's word (§3.7's `stop`), as pairs: an unknown one is drawn as served. */
const STOP_WORD: ReadonlyMap<string, string> = new Map([
  ['iam', 'IAM'],
  ['contract', 'Frozen contract'],
  ['protected_path', 'Protected path'],
  ['security', 'Security'],
  ['workflows', 'Workflows'],
  ['secrets', 'Secret material'],
  ['tenancy', 'Tenancy'],
  ['shared', 'Shared project'],
  ['cost', 'Cost'],
])

interface DocStep {
  id: string
  title: string
  prompt: string | null
  files: string[]
  tests: string[]
  estimate: string | null
  doneWhen: string[]
  meets: number[]
  reason: string | null
}

function stepsOf(plan: Json): DocStep[] {
  return objs(plan.steps).flatMap((o, i) => {
    const title = text(o.title) ?? text(o.prompt)
    if (title === null) return []
    return [
      {
        id: text(o.step_id) ?? `step-${i + 1}`,
        title,
        prompt: text(o.prompt),
        files: texts(o.files),
        tests: texts(o.tests),
        estimate: text(o.estimate),
        doneWhen: texts(o.done_when),
        meets: Array.isArray(o.requirements) ? o.requirements.filter((n): n is number => typeof n === 'number') : [],
        reason: text(o.rationale),
      },
    ]
  })
}

/** One part of the document beside its margin: the planner's reason, then the part's thread (U4). */
function Part({ label, anchor, reason, thread, children }: { label: string; anchor: string; reason: string | null; thread?: (anchor: string, label: string) => ReactNode; children: ReactNode }) {
  const said = thread?.(anchor, label) ?? null
  return (
    <section className="pd-part" aria-label={label} data-anchor={anchor}>
      <div className="pd-main">{children}</div>
      <aside className="pd-margin" aria-label={`${label}: the planner’s reason`}>
        {reason !== null && (
          <p className="pd-reason" data-testid="pd-reason">
            {reason}
          </p>
        )}
        {said}
      </aside>
    </section>
  )
}

function List({ label, items, mono = false }: { label: string; items: string[]; mono?: boolean }) {
  return (
    <div className="pd-kv">
      <span className="pd-k">{label}</span>
      <ul className="pd-list" aria-label={label}>
        {items.map((x, i) => (
          <li key={`${i}:${x}`} className={mono ? 'pd-mono' : undefined} title={mono ? x : undefined}>
            {x}
          </li>
        ))}
      </ul>
    </div>
  )
}

function RiskRows({ rows }: { rows: Json[] }) {
  return (
    <ul className="pd-risk" aria-label="Risk">
      {rows.map((r, i) => {
        const stop = text(r.stop) ?? 'stop'
        const matched = texts(r.matched)
        return (
          <li key={`${stop}:${i}`} data-stop={stop}>
            <span className="pd-stop">{STOP_WORD.get(stop) ?? stop}</span>
            {matched.map((m) => (
              <code key={m} className="pd-mono pd-dim" title={m}>
                {m}
              </code>
            ))}
          </li>
        )
      })}
    </ul>
  )
}

const usd = (n: number) => `$${n.toFixed(2)}`

function Cost({ cost }: { cost: Json | null | undefined }) {
  if (cost === undefined) return null
  const c = obj(cost)
  const low = num(c?.low)
  const high = num(c?.high)
  return (
    <p className="pd-cost" data-testid="pd-cost">
      <span className="pd-k">Expected cost</span>
      {low !== null && high !== null ? (
        <>
          <b>{low === high ? usd(low) : `${usd(low)}–${usd(high)}`}</b>
          {text(c?.basis) !== null && <InfoTip about="the expected cost" tip={`${text(c?.basis)}${text(c?.prices_at) === null ? '' : ` · prices at ${text(c?.prices_at)}`}`} />}
        </>
      ) : (
        <Dash why={text(c?.why_none) ?? 'not served'} />
      )}
    </p>
  )
}

/** The run read's state, said in one mark: stale with its age, failed with its heading. */
function ReadState({ read, onRead }: { read: Result<unknown> | null; onRead: () => void }) {
  const now = useNow()
  if (read === null || read.status === 'ok') return null
  if (read.status === 'stale')
    return (
      <p className="pd-state is-stale" data-testid="pd-state" data-state="stale">
        <span className="pd-dim">read {timeAgo(new Date(read.fetchedAt).toISOString(), now)}</span>
        <InfoTip about="an old read" tip={`The run’s latest read failed (${errorHeading(read.error)}); this is the last one that answered.`} />
      </p>
    )
  if (read.status === 'error')
    return (
      <p className="pd-state is-error" role="alert" data-testid="pd-state" data-state="error">
        <span>Run not read: {errorHeading(read.error)}</span>
        <Button kind="ghost" size="sm" onClick={onRead}>
          Retry
        </Button>
      </p>
    )
  return (
    <p className="pd-state" data-testid="pd-state" data-state={read.status}>
      <span className="pd-dim">{read.status === 'loading' ? 'Reading…' : 'Run answered nothing'}</span>
    </p>
  )
}

/**
 * The plan as a document with its margin. `plan` is the revision shown (null:
 * none served); `run` is the run read's fields beside it (`plan_risk`,
 * `expected_cost_usd`), null when the run was not read; `read` is that read's
 * Result, so loading, empty, stale and error stay apart.
 */
export function PlanDocument({
  plan,
  run,
  read,
  digest,
  onRead,
  pinned = null,
  thread,
}: {
  plan: Json | null
  run: Json | null
  read: Result<unknown> | null
  digest: string | null
  onRead: () => void
  pinned?: ReactNode
  thread?: (anchor: string, label: string) => ReactNode
}) {
  // The margin's thread, Comment, Request changes and the revisions (U4,
  // PlanThread.tsx and PlanRevisions.tsx), from the run read and kept live
  // while a re-plan runs. A run read with no `plan_thread` draws none.
  const runId = run === null ? null : text(run.id)
  const conversation = usePlanConversation(runId, run)
  const shownRev = shownRevision(digest, run).revision
  useBarRegistration(
    conversation.thread === null || runId === null || digest === null
      ? null
      : {
          runId,
          digest,
          revision: shownRev,
          open: openComments(conversation.thread, digest).length,
          replan: conversation.replan,
          mayEdit: conversation.may.edit && run?.state === 'PLANNED',
          onReplan: conversation.setReplan,
        },
  )
  if (plan === null)
    return (
      <section className="pd-doc" aria-label="The plan" data-testid="pd-doc" data-state="empty">
        <ReadState read={read} onRead={onRead} />
        <p className="pd-dim" data-testid="pd-empty">
          No plan served
        </p>
      </section>
    )
  const steps = stepsOf(plan)
  const requirements = texts(plan.requirements)
  // The risk rows are drawn only for the digest they were computed for (§3.7).
  const risk = obj(run?.plan_risk)
  const riskSteps = risk !== null && (digest === null || risk.digest === digest) ? obj(risk.steps) : null
  const [first, ...rest] = requirements
  const margin =
    thread ??
    ((anchor: string, label: string) => <PlanThread c={conversation} anchorKey={anchor} label={label} plan={plan} digest={digest} shownRevision={shownRev} />)
  return (
    <section className="pd-doc" aria-label="The plan" data-testid="pd-doc" data-state={read?.status ?? 'ok'}>
      <ReadState read={read} onRead={onRead} />
      {pinned}
      {runId !== null && conversation.revisions.length > 0 && (
        <PlanRevisionPicker runId={runId} revisions={conversation.revisions} shown={shownRev} shownPlan={plan} complete={conversation.complete} />
      )}
      <Part label="Summary" anchor="plan" reason={text(plan.rationale)} thread={margin}>
        <p className="pd-summary" data-testid="pd-summary">
          {text(plan.summary) ?? <Dash why="no summary served" />}
        </p>
        <div className="pd-facts">
          {text(plan.estimate) !== null && (
            <p className="pd-est">
              <span className="pd-k">Estimate</span>
              <b>{text(plan.estimate)}</b>
            </p>
          )}
          <Cost cost={run === null ? undefined : (run.expected_cost_usd as Json | null | undefined)} />
        </div>
      </Part>
      {first !== undefined && (
        <Part label="Requirements" anchor="requirements" reason={null} thread={margin}>
          <h3 className="pd-h">Requirements</h3>
          <ol className="pd-reqs" aria-label="Requirements" data-testid="pd-reqs">
            <li>{first}</li>
          </ol>
          {rest.length > 0 && (
            <details className="pd-more">
              <summary>{rest.length} more</summary>
              <ol className="pd-reqs" start={2} aria-label="More requirements">
                {rest.map((r, i) => (
                  <li key={`${i}:${r}`}>{r}</li>
                ))}
              </ol>
            </details>
          )}
        </Part>
      )}
      {steps.map((s, i) => {
        const rows = riskSteps === null ? [] : objs(riskSteps[s.id])
        return (
          <Part key={s.id} label={`Step ${i + 1}`} anchor={`step:${s.id}`} reason={s.reason} thread={margin}>
            <h3 className="pd-step-h">
              <span className="pd-stepn">{i + 1}</span>
              <span className="pd-step-t">{s.title}</span>
              {s.estimate !== null && <span className="pd-dim pd-step-e">{s.estimate}</span>}
            </h3>
            <p className="pd-step-m">
              <code className="pd-mono pd-dim">{s.id}</code>
              {s.meets.length > 0 && <span className="pd-dim">{s.meets.map((n) => `R${n}`).join(' ')}</span>}
            </p>
            {rows.length > 0 && <RiskRows rows={rows} />}
            {s.files.length > 0 && <List label="Files" items={s.files} mono />}
            {s.tests.length > 0 && <List label="Tests" items={s.tests} />}
            {s.doneWhen.length > 0 && <List label="Done when" items={s.doneWhen} />}
            {s.prompt !== null && s.prompt !== s.title && (
              <details className="pd-more">
                <summary>Prompt</summary>
                <pre className="pd-prompt">{s.prompt}</pre>
              </details>
            )}
          </Part>
        )
      })}
    </section>
  )
}
