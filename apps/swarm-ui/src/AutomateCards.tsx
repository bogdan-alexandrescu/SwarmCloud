// MOCK-HOOK FILE
// Automate redesign, direction "cards" (mock-up only, lane build-cards): each
// schedule is a card in the Subscription accounts style, leading with its next
// firing, the state word only where it asks for something, and its spend
// against its tripwire, with one Manage per card. Drawn at ?automate=cards
// &frame=<frame> on /automate/schedules and /automate/approvals. Every figure
// is invented fixture data; nothing here reads or writes the API.
import { useState, type ReactNode } from 'react'
import './styles/automate-cards.css'
import { Breadcrumb, Button, EmptyState, Segmented, Tag } from './components'
import { Info } from './AccountsVerdict'

export function automateCardsFrame(): string | null {
  const q = new URLSearchParams(window.location.search)
  if (q.get('automate') !== 'cards') return null
  return q.get('frame') ?? 'landing'
}

/* ---------------------------------------------------------------- fixtures */

type Word = 'active' | 'paused' | 'paused_by_tripwire' | 'waiting_on_approval' | 'failing' | 'never_fired'
type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'paused'

interface Sched {
  id: string
  name: string
  type: string
  target: string
  cadence: string
  cron: string
  word: Word
  next: { at: string; in: string } | null
  noNext?: string
  spend: number | null
  spendWhy?: string
  perDay: number
  perRun: number
  last: { outcome: 'succeeded' | 'failed' | 'partial' | 'skipped'; at: string } | null
  waiting?: number
  failures?: number
}

const USD = (n: number) => `$${n.toFixed(2)}`

const FAILURE_REVIEW: Sched = {
    id: 'sch_4c7e19a0b2d1', name: 'Failure review', type: 'failure-review', target: 'example-org/release-tools', cadence: 'Every day at 09:00',
    cron: '0 9 * * *', word: 'waiting_on_approval', next: { at: 'Oct 20 09:00 UTC', in: 'in 21 h' }, spend: 3.4, perDay: 10, perRun: 4,
    last: { outcome: 'succeeded', at: 'today 09:00' }, waiting: 1,
}

const SCHEDULES: readonly Sched[] = [
  FAILURE_REVIEW,
  {
    id: 'sch_91b3f0d6a7e2', name: 'Nightly dependency bump', type: 'dependency-bump', target: '3 repositories', cadence: 'Every day at 02:30',
    cron: '30 2 * * *', word: 'paused_by_tripwire', next: null, noNext: 'paused by its tripwire', spend: 12.1, perDay: 12, perRun: 4,
    last: { outcome: 'partial', at: 'today 02:30' },
  },
  {
    id: 'sch_2a8d5e7c3f14', name: 'Flaky test sweep', type: 'issue-run', target: 'example-org/web-app', cadence: 'Every 6 hours',
    cron: '0 */6 * * *', word: 'failing', next: { at: 'Oct 19 18:00 UTC', in: 'in 6 h' }, spend: 5.85, perDay: 15, perRun: 3,
    last: { outcome: 'failed', at: 'today 12:00' }, failures: 3,
  },
  {
    id: 'sch_3f9a0c1e7b25', name: 'Plan open issues', type: 'issue-plan-only', target: 'example-org/web-app', cadence: 'Weekdays at 06:00',
    cron: '0 6 * * 1-5', word: 'active', next: { at: 'Oct 20 06:00 UTC', in: 'in 18 h' }, spend: 1.2, perDay: 6, perRun: 2,
    last: { outcome: 'succeeded', at: 'today 06:00' },
  },
  {
    id: 'sch_6e0b2c9d4a83', name: 'Docs link check', type: 'issue-run', target: 'example-org/docs-site', cadence: 'Every hour',
    cron: '0 * * * *', word: 'active', next: { at: 'Oct 19 13:00 UTC', in: 'in 1 h' }, spend: 0.64, perDay: 4, perRun: 0.5,
    last: { outcome: 'succeeded', at: 'today 12:00' },
  },
  {
    id: 'sch_8b2e6d4a1c90', name: 'Weekly cost report', type: 'cost-report', target: 'All repositories', cadence: 'Mondays at 08:00',
    cron: '0 8 * * 1', word: 'paused', next: null, noNext: 'paused', spend: 0, perDay: 1, perRun: 0.5,
    last: { outcome: 'succeeded', at: 'Oct 12 08:00' },
  },
  {
    id: 'sch_c51d7a2e9b46', name: 'Release notes draft', type: 'issue-plan-only', target: 'example-org/release-tools', cadence: 'Fridays at 15:00',
    cron: '0 15 * * 5', word: 'never_fired', next: { at: 'Oct 23 15:00 UTC', in: 'in 4 d' }, spend: 0, perDay: 5, perRun: 2.5, last: null,
  },
]

/** The word, only where the schedule asks for something; the others say nothing. */
const ASK: Partial<Record<Word, { word: string; tone: Tone; tip: string }>> = {
  waiting_on_approval: { word: 'Waiting on approval', tone: 'info', tip: 'A firing holds work until an approver decides. It expires if nobody does.' },
  paused_by_tripwire: { word: 'Paused by its tripwire', tone: 'warn', tip: 'Today’s spend reached the per-day budget, so the schedule paused itself. It stays paused until someone resumes it.' },
  failing: { word: 'Failing', tone: 'bad', tip: 'The last firings failed in a row. At the limit the schedule pauses itself.' },
}

interface Appr {
  id: string
  kind: 'merge' | 'plan' | 'run'
  title: string
  from: string
  requested: string
  expires: string
  state: 'pending' | 'approved' | 'denied' | 'expired' | 'superseded'
  by?: string
  at?: string
}

const MERGE: Appr = { id: 'apr_0b7e3d1c9a52', kind: 'merge', title: 'Merge PR #482 · Retry the release upload once', from: 'Failure review', requested: 'today 09:14', expires: 'in 2 d 21 h', state: 'pending' }

const APPROVALS: readonly Appr[] = [
  MERGE,
  { id: 'apr_5d2c8a6f1e30', kind: 'plan', title: 'Plan for #317 · Paginate the audit export', from: 'Plan open issues', requested: 'today 06:08', expires: 'in 2 d 18 h', state: 'pending' },
  { id: 'apr_e9a41b7c0d28', kind: 'run', title: 'Run on #322 · Drop the unused theme tokens', from: 'Plan open issues', requested: 'today 06:09', expires: 'in 2 d 18 h', state: 'pending' },
  { id: 'apr_71f0c3b8e6d4', kind: 'plan', title: 'Plan for #309 · Cache the sitemap build', from: 'Plan open issues', requested: 'Oct 18 06:07', expires: '', state: 'approved', by: 'Alice', at: 'Oct 18 07:12' },
  { id: 'apr_3c96e2a0f5b7', kind: 'merge', title: 'Merge PR #471 · Bump the docs toolchain', from: 'Nightly dependency bump', requested: 'Oct 18 02:41', expires: '', state: 'denied', by: 'Alice', at: 'Oct 18 08:30' },
  { id: 'apr_a2d7f49c1e83', kind: 'merge', title: 'Merge PR #468 · Pin the image digest', from: 'Failure review', requested: 'Oct 17 09:12', expires: '', state: 'superseded', at: 'Oct 17 14:20' },
  { id: 'apr_6b1e0f8d2c97', kind: 'run', title: 'Run on #301 · Retire the old banner', from: 'Plan open issues', requested: 'Oct 14 06:10', expires: 'Oct 17 06:10', state: 'expired' },
]

/* ------------------------------------------------------------------- parts */

function Mark({ tone }: { tone: Tone }) {
  return <span className={`acd-dot is-${tone}`} aria-hidden />
}

function Word({ s }: { s: Sched }) {
  const a = ASK[s.word]
  if (a === undefined) return null
  return (
    <span className={`acd-word is-${a.tone}`}>
      <Mark tone={a.tone} />
      {a.word}
      {s.failures !== undefined && <span className="acd-dim"> &middot; {s.failures} in a row</span>}
      <Info about={a.word} tip={a.tip} />
    </span>
  )
}

/** Spend against the tripwire: the bar fills to the per-day budget, which is the tripwire. */
function Spend({ used, cap, why, big = false }: { used: number | null; cap: number; why?: string; big?: boolean }) {
  if (used === null) {
    return (
      <div className={`acd-spend${big ? ' is-big' : ''}`}>
        <div className="acd-spend-t">
          <span className="acd-fig">&mdash;</span>
          <span className="acd-dim">of {USD(cap)} tripwire</span>
          <Info about="Spend unreadable" tip={why ?? 'Not read.'} />
        </div>
        <span className="acd-bar is-unknown" aria-label="spend not read" />
        <span className="acd-why">{why}</span>
      </div>
    )
  }
  const pct = Math.min(100, (used / cap) * 100)
  const tone = pct >= 100 ? 'is-bad' : pct >= 80 ? 'is-warn' : ''
  return (
    <div className={`acd-spend${big ? ' is-big' : ''}`}>
      <div className="acd-spend-t">
        <span className="acd-fig">{USD(used)}</span>
        <span className="acd-dim">of {USD(cap)} tripwire today</span>
      </div>
      <span className={`acd-bar ${tone}`} role="meter" aria-valuenow={used} aria-valuemin={0} aria-valuemax={cap} aria-label="spend today against the tripwire">
        <i style={{ width: `${pct}%` }} />
      </span>
    </div>
  )
}

function Outcome({ o }: { o: NonNullable<Sched['last']>['outcome'] }) {
  const tone: Tone = o === 'succeeded' ? 'ok' : o === 'failed' ? 'bad' : o === 'partial' ? 'warn' : 'paused'
  return (
    <span className="acd-out">
      <Mark tone={tone} />
      {o}
    </span>
  )
}

function ScheduleCard({ s, unread }: { s: Sched; unread?: boolean }) {
  const ask = ASK[s.word]
  return (
    <section className={`acd-card${ask === undefined ? '' : ` is-${ask.tone}`}`} aria-label={s.name}>
      <div className="acd-card-h">
        <Tag>{s.type}</Tag>
        <h3>{s.name}</h3>
      </div>
      <Word s={s} />
      <div className="acd-next">
        <span className="acd-k">Next firing</span>
        {s.next === null ? (
          <span className="acd-big is-none">&mdash; {s.noNext}</span>
        ) : (
          <span className="acd-next-v">
            <span className="acd-big">{s.next.in}</span>
            <span className="acd-dim">{s.next.at}</span>
          </span>
        )}
      </div>
      <Spend used={unread === true ? null : s.spend} cap={s.perDay} why={unread === true ? 'spend ledger timed out' : undefined} />
      <p className="acd-line">
        <span>{s.target}</span>
        <span className="acd-dim">{s.cadence}</span>
      </p>
      <div className="acd-card-f">
        {s.last === null ? <span className="acd-dim">never fired</span> : <span className="acd-dim"><Outcome o={s.last.outcome} /> {s.last.at}</span>}
        <Button size="sm">Manage</Button>
      </div>
    </section>
  )
}

function WaitingRows({ rows }: { rows: readonly Appr[] }) {
  return (
    <ul className="acd-wrows">
      {rows.map((a) => (
        <li key={a.id}>
          <Tag>{KIND_WORD[a.kind]}</Tag>
          <span className="acd-wt">{a.title}</span>
          <span className="acd-dim">{a.from}</span>
          <span className="acd-exp"><span className="acd-k">Expires</span> <span className="acd-strong">{a.expires}</span></span>
          <Button size="sm" kind="primary">Review</Button>
        </li>
      ))}
    </ul>
  )
}

function Tile({ k, children, sub, tip }: { k: string; children: ReactNode; sub?: ReactNode; tip?: string }) {
  return (
    <div className="acd-tile">
      <span className="acd-k">
        {k}
        {tip !== undefined && <Info about={k} tip={tip} />}
      </span>
      <span className="acd-tile-v">{children}</span>
      {sub !== undefined && <span className="acd-dim">{sub}</span>}
    </div>
  )
}

const KIND_WORD: Record<Appr['kind'], string> = { merge: 'Merge', plan: 'Plan', run: 'Run' }

function ApprovalCard({ a }: { a: Appr }) {
  const decided = a.state !== 'pending'
  return (
    <section className={`acd-acard${a.state === 'pending' ? ' is-info' : ''}`} aria-label={a.title}>
      <div className="acd-card-h">
        <Tag>{KIND_WORD[a.kind]}</Tag>
        <h3>{a.title}</h3>
      </div>
      <dl className="acd-facts">
        <dt>From</dt>
        <dd>{a.from}</dd>
        <dt>Asked</dt>
        <dd>{a.requested}</dd>
        {a.state === 'pending' && (
          <>
            <dt>Expires</dt>
            <dd className="acd-strong">{a.expires}</dd>
          </>
        )}
        {a.state === 'expired' && (
          <>
            <dt>Expired</dt>
            <dd>{a.expires}</dd>
          </>
        )}
        {(a.state === 'approved' || a.state === 'denied') && (
          <>
            <dt>{a.state === 'approved' ? 'Approved by' : 'Denied by'}</dt>
            <dd>{a.by}</dd>
            <dt>Decided</dt>
            <dd>{a.at}</dd>
          </>
        )}
        {a.state === 'superseded' && (
          <>
            <dt>Superseded</dt>
            <dd>{a.at} &middot; head moved</dd>
          </>
        )}
      </dl>
      <div className="acd-card-f">
        <Button size="sm" kind={decided ? 'secondary' : 'primary'}>{decided ? 'Open' : 'Review'}</Button>
      </div>
    </section>
  )
}

/* -------------------------------------------------------------- landing */

type Filter = 'all' | 'needs' | 'active' | 'paused' | 'never'

function Landing({ unread = false }: { unread?: boolean }) {
  const [f, setF] = useState<Filter>('all')
  const needs = SCHEDULES.filter((s) => ASK[s.word] !== undefined)
  const shown = SCHEDULES.filter((s) =>
    f === 'all' ? true : f === 'needs' ? ASK[s.word] !== undefined : f === 'active' ? s.word === 'active' : f === 'paused' ? s.word === 'paused' : s.word === 'never_fired',
  )
  const waiting = APPROVALS.filter((a) => a.state === 'pending').sort((x, y) => x.requested.localeCompare(y.requested))
  return (
    <div className="acd-page">
      <div className="acd-answer">
        <Tile k="Needs you" sub="1 approval · 1 tripwire · 1 failing">
          <span className="acd-huge">{needs.length}</span>
          <span className="acd-dim"> of {SCHEDULES.length} schedules</span>
        </Tile>
        <Tile k="Next firing" sub="Docs link check · Oct 19 13:00 UTC">
          in 1 h
        </Tile>
        <Tile k="Spend today" sub={unread ? 'spend ledger timed out' : 'of $53.00 across every tripwire'} tip="Reported spend since 00:00 UTC, summed over every schedule. Each schedule pauses itself at its own per-day budget.">
          {unread ? <span title="spend ledger timed out">&mdash;</span> : '$23.23'}
        </Tile>
      </div>
      <section className="acd-panel is-tight" aria-label="Waiting on you">
        <div className="acd-panel-h">
          <h2>Waiting on you</h2>
          <span className="acd-dim">{waiting.length}, oldest first</span>
          <span className="acd-grow" />
          <Button size="sm">All approvals</Button>
        </div>
        <WaitingRows rows={waiting} />
      </section>
      <div className="acd-bar-row">
        <Segmented<Filter>
          label="Show"
          value={f}
          onChange={setF}
          options={[
            { key: 'all', label: 'All', count: SCHEDULES.length },
            { key: 'needs', label: 'Needs you', count: needs.length },
            { key: 'active', label: 'Active', count: 2 },
            { key: 'paused', label: 'Paused', count: 1 },
            { key: 'never', label: 'Never fired', count: 1 },
          ]}
        />
        <span className="acd-grow" />
        <Button kind="primary">New schedule</Button>
      </div>
      <div className="acd-grid">
        {shown.map((s) => (
          <ScheduleCard key={s.id} s={s} unread={unread} />
        ))}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- schedule */

const FIRINGS = [
  { at: 'Oct 19 09:00', trig: 'cron', out: 'succeeded' as const, work: 'PR #482 opened · merge waiting', cost: 3.4 },
  { at: 'Oct 18 09:00', trig: 'cron', out: 'succeeded' as const, work: '2 issues filed', cost: 2.15 },
  { at: 'Oct 17 14:22', trig: 'run now', out: 'partial' as const, work: 'PR #468 · superseded', cost: 2.5 },
  { at: 'Oct 17 09:00', trig: 'cron', out: 'skipped' as const, work: 'nothing failed overnight', cost: 0 },
  { at: 'Oct 16 09:00', trig: 'cron', out: 'failed' as const, work: 'checkout refused', cost: 0.42 },
  { at: 'Oct 15 09:00', trig: 'cron', out: 'succeeded' as const, work: '1 issue filed', cost: 1.9 },
]

const WEEK = [1.1, 0.85, 1.9, 0.42, 2.5, 2.15, 3.4]

function ScheduleFrame() {
  const s = FAILURE_REVIEW
  return (
    <div className="acd-page">
      <Breadcrumb crumbs={[{ key: 's', label: 'Schedules', href: '#' }, { key: 'n', label: s.name, href: null }]} />
      <section className="acd-head is-info" aria-label={s.name}>
        <div className="acd-head-t">
          <div className="acd-card-h">
            <h2>{s.name}</h2>
            <Tag>{s.type}</Tag>
            <Info about="What it does" tip="Reads last night's failed runs, files an issue per new failure and opens a fix pull request where the fix is small." />
          </div>
          <Word s={s} />
          <dl className="acd-facts is-row">
            <div className="acd-pair"><dt>Target</dt><dd>{s.target}</dd></div>
            <div className="acd-pair"><dt>Cadence</dt><dd>{s.cadence} UTC</dd></div>
            <div className="acd-pair"><dt>Cron</dt><dd className="acd-mono">{s.cron}</dd></div>
            <div className="acd-pair"><dt>Owner</dt><dd>Alice</dd></div>
            <div className="acd-pair"><dt>Id</dt><dd className="acd-mono">{s.id}</dd></div>
          </dl>
        </div>
        <div className="acd-acts">
          <Button kind="primary">Run now</Button>
          <Button>Edit</Button>
          <Button>Pause</Button>
          <Button kind="danger">Delete</Button>
        </div>
      </section>
      <div className="acd-cols">
        <section className="acd-panel" aria-label="Next firings">
          <div className="acd-panel-h"><h2>Next firings</h2></div>
          <div className="acd-next is-lead">
            <span className="acd-big">in 21 h</span>
            <span className="acd-dim">Oct 20 09:00 UTC</span>
          </div>
          <ol className="acd-list">
            {['Oct 20 09:00', 'Oct 21 09:00', 'Oct 22 09:00', 'Oct 23 09:00', 'Oct 24 09:00'].map((t, i) => (
              <li key={t} className={`acd-tab${i === 0 ? ' acd-strong' : ''}`}>{t} UTC</li>
            ))}
          </ol>
        </section>
        <section className="acd-panel" aria-label="Spend">
          <div className="acd-panel-h">
            <h2>Spend</h2>
            <Info about="Tripwire" tip="At the per-day budget the schedule pauses itself until someone resumes it. A firing that would pass the per-run budget is stopped." />
          </div>
          <Spend used={s.spend} cap={s.perDay} big />
          <dl className="acd-facts">
            <dt>Per run</dt>
            <dd>{USD(s.perRun)} cap</dd>
            <dt>Unreported</dt>
            <dd>0 attempts</dd>
          </dl>
          <div className="acd-week" role="img" aria-label="spend on each of the last 7 days">
            {WEEK.map((v, i) => (
              <span key={i} className="acd-week-d" title={`Oct ${13 + i} · ${USD(v)}`}>
                <i style={{ height: `${Math.max(2, (v / s.perDay) * 100)}%` }} />
                <small>{['13', '14', '15', '16', '17', '18', '19'][i]}</small>
              </span>
            ))}
          </div>
        </section>
        <section className="acd-panel" aria-label="Gates and approvers">
          <div className="acd-panel-h">
            <h2>Gates</h2>
            <Info about="Gates" tip="Auto lets the step go on; approve holds it for an approver. A gate is never lower than the type's floor." />
          </div>
          <dl className="acd-facts">
            <dt>Plan</dt>
            <dd>auto</dd>
            <dt>Run</dt>
            <dd>auto</dd>
            <dt>Merge</dt>
            <dd className="acd-strong">approve</dd>
            <dt>Approvers</dt>
            <dd>owner only</dd>
            <dt>Expires after</dt>
            <dd>72 h</dd>
          </dl>
        </section>
      </div>
      <div className="acd-cols is-wide">
      <section className="acd-panel" aria-label="Firings">
        <div className="acd-panel-h">
          <h2>Firings</h2>
          <span className="acd-dim">last 6</span>
          <span className="acd-grow" />
          <Button size="sm">Older</Button>
        </div>
        <ul className="acd-firings">
          {FIRINGS.map((r) => (
            <li key={r.at}>
              <span className="acd-tab">{r.at}</span>
              <span className="acd-dim">{r.trig}</span>
              <Outcome o={r.out} />
              <span className="acd-work">{r.work}</span>
              <span className="acd-tab acd-num">{USD(r.cost)}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="acd-panel" aria-label="Waiting on this schedule">
        <div className="acd-panel-h"><h2>Waiting</h2><span className="acd-dim">1</span></div>
        <div className="acd-stack">
          <ApprovalCard a={MERGE} />
        </div>
      </section>
      </div>
    </div>
  )
}

/* ----------------------------------------------------------------- edit */

const TEMPLATES = [
  { name: 'failure-review', tier: 'R1' },
  { name: 'issue-plan-only', tier: 'R1' },
  { name: 'issue-run', tier: 'R2' },
  { name: 'dependency-bump', tier: 'R2' },
  { name: 'cost-report', tier: 'R0' },
]

const TIER_WORDS: Record<string, string> = {
  R0: 'R0: SwarmCloud records and the workspace’s own objects',
  R1: 'R1: issues, comments and labels; no code',
  R2: 'R2: code on branches through pull requests; no merge',
}
const TIER_TIP = `What the type may change. ${Object.values(TIER_WORDS).join('. ')}.`

function Field({ label, children, tip, error }: { label: string; children: ReactNode; tip?: string; error?: string }) {
  return (
    <label className={`acd-field${error === undefined ? '' : ' is-bad'}`}>
      <span className="acd-k">
        {label}
        {tip !== undefined && <Info about={label} tip={tip} />}
      </span>
      {children}
      {error !== undefined && <span className="acd-err" role="alert">{error}</span>}
    </label>
  )
}

function EditFrame() {
  return (
    <div className="acd-page">
      <Breadcrumb crumbs={[{ key: 's', label: 'Schedules', href: '#' }, { key: 'n', label: 'Failure review', href: '#' }, { key: 'e', label: 'Edit', href: null }]} />
      <div className="acd-check is-bad" role="status">
        <Mark tone="bad" />
        <span className="acd-strong">1 field to fix</span>
        <span className="acd-dim">Per day</span>
        <span className="acd-grow" />
        <span className="acd-dim">revision 7</span>
        <Button>Cancel</Button>
        <Button kind="primary" disabled>Save</Button>
      </div>
      <div className="acd-edit">
        <div className="acd-stack">
          <section className="acd-panel" aria-label="Type">
            <div className="acd-panel-h">
              <h2>Type</h2>
              <Info about="Tier" tip={TIER_TIP} />
            </div>
            <div className="acd-tpls" role="radiogroup" aria-label="Type">
              {TEMPLATES.map((t) => (
                <button key={t.name} type="button" role="radio" aria-checked={t.name === 'failure-review'} className={`acd-tpl${t.name === 'failure-review' ? ' is-on' : ''}`}>
                  <span className="acd-mono">{t.name}</span>
                  <span className="acd-dim" title={TIER_WORDS[t.tier]}>{t.tier}</span>
                </button>
              ))}
            </div>
            <div className="acd-row2">
            <Field label="Name"><input className="acd-in" defaultValue="Failure review" /></Field>
            <Field label="Start" tip="Dry run first fires and records what it would do, and creates nothing.">
              <Segmented label="Start" value="enabled" options={[{ key: 'enabled', label: 'Enabled' }, { key: 'dry_run', label: 'Dry run first' }, { key: 'paused', label: 'Paused' }]} />
            </Field>
          </div>
          </section>
          <section className="acd-panel" aria-label="Gates">
            <div className="acd-panel-h">
              <h2>Gates</h2>
              <Info about="Gate floor" tip="failure-review cannot merge without approval: its merge gate's floor is approve." />
            </div>
            <div className="acd-gates">
              <span className="acd-k">Plan</span>
              <Segmented label="Plan" small value="auto" options={[{ key: 'auto', label: 'Auto' }, { key: 'approve', label: 'Approve' }]} />
              <span className="acd-k">Run</span>
              <Segmented label="Run" small value="auto" options={[{ key: 'auto', label: 'Auto' }, { key: 'approve', label: 'Approve' }]} />
              <span className="acd-k">Merge</span>
              <Segmented label="Merge" small value="approve" options={[{ key: 'off', label: 'Off' }, { key: 'approve', label: 'Approve' }]} />
            </div>
            <div className="acd-row2">
              <Field label="Approvers">
                <select className="acd-in" defaultValue="owner">
                  <option value="owner">Owner only</option>
                  <option value="admins">Workspace admins</option>
                  <option value="members">Any member</option>
                </select>
              </Field>
              <Field label="Expires after"><input className="acd-in" defaultValue="72 h" /></Field>
            </div>
          </section>
        </div>
        <div className="acd-stack">
          <section className="acd-panel" aria-label="Target">
            <div className="acd-panel-h"><h2>Target</h2></div>
            <Segmented label="Scope" value="repos" options={[{ key: 'repos', label: 'Repositories' }, { key: 'all', label: 'All' }]} />
            <div className="acd-chips">
              <Tag>example-org/release-tools</Tag>
              <Button size="sm">Add repository</Button>
            </div>
          </section>
          <section className="acd-panel" aria-label="Cadence">
            <div className="acd-panel-h"><h2>Cadence</h2></div>
            <div className="acd-row2">
              <Field label="Cron" tip="Five fields: minute, hour, day of month, month, day of week."><input className="acd-in acd-mono" defaultValue="0 9 * * *" /></Field>
              <Field label="Time zone"><input className="acd-in" defaultValue="UTC" /></Field>
            </div>
            <span className="acd-strong">Every day at 09:00</span>
            <div className="acd-preview">
              <span className="acd-k">Next 5</span>
              <ol className="acd-list">
                {['Oct 20 09:00', 'Oct 21 09:00', 'Oct 22 09:00', 'Oct 23 09:00', 'Oct 24 09:00'].map((t) => (
                  <li key={t} className="acd-tab">{t} UTC</li>
                ))}
              </ol>
              <span className="acd-dim">min gap 24 h &middot; type allows 60 min</span>
            </div>
          </section>
          <section className="acd-panel" aria-label="Budget">
            <div className="acd-panel-h">
              <h2>Budget</h2>
              <Info about="Tripwire" tip="Per day is the tripwire: at it the schedule pauses itself." />
            </div>
            <div className="acd-row2">
              <Field label="Per run"><input className="acd-in" defaultValue="$4.00" /></Field>
              <Field label="Per day" error="above the type's cap of $30.00"><input className="acd-in" defaultValue="$45.00" aria-invalid /></Field>
              <Field label="At once"><input className="acd-in" defaultValue="1" /></Field>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ approvals */

type Kind = 'all' | Appr['kind']

function ApprovalsFrame({ none = false }: { none?: boolean }) {
  const [k, setK] = useState<Kind>('all')
  const pick = (a: Appr) => k === 'all' || a.kind === k
  const waiting = none ? [] : APPROVALS.filter((a) => a.state === 'pending' && pick(a)).sort((x, y) => x.requested.localeCompare(y.requested))
  const decided = APPROVALS.filter((a) => (a.state === 'approved' || a.state === 'denied') && pick(a))
  const expired = APPROVALS.filter((a) => a.state === 'expired' && pick(a))
  const superseded = APPROVALS.filter((a) => a.state === 'superseded' && pick(a))
  return (
    <div className="acd-page">
      <div className="acd-bar-row">
        <Segmented<Kind>
          label="Kind"
          value={k}
          onChange={setK}
          options={[
            { key: 'all', label: 'All' },
            { key: 'merge', label: 'Merge' },
            { key: 'plan', label: 'Plan' },
            { key: 'run', label: 'Run' },
          ]}
        />
      </div>
      <section className="acd-panel" aria-label="Waiting">
        <div className="acd-panel-h">
          <h2>Waiting</h2>
          <span className="acd-dim">{waiting.length === 0 ? '' : `${waiting.length}, oldest first`}</span>
        </div>
        {waiting.length === 0 ? (
          <p className="acd-nothing"><Mark tone="ok" /> Nothing waiting</p>
        ) : (
          <div className="acd-grid is-3">
            {waiting.map((a) => (
              <ApprovalCard key={a.id} a={a} />
            ))}
          </div>
        )}
      </section>
      <div className="acd-lapsed">
        <section className="acd-panel" aria-label="Decided">
          <div className="acd-panel-h"><h2>Decided</h2><span className="acd-dim">{decided.length}</span></div>
          <div className="acd-grid is-2">
            {decided.map((a) => (
              <ApprovalCard key={a.id} a={a} />
            ))}
          </div>
        </section>
        <section className="acd-panel" aria-label="Expired">
          <div className="acd-panel-h"><h2>Expired</h2><span className="acd-dim">{expired.length}</span></div>
          <div className="acd-stack">
            {expired.map((a) => (
              <ApprovalCard key={a.id} a={a} />
            ))}
          </div>
        </section>
        <section className="acd-panel" aria-label="Superseded">
          <div className="acd-panel-h">
            <h2>Superseded</h2>
            <span className="acd-dim">{superseded.length}</span>
            <Info about="Superseded" tip="The head moved after the approval was asked for, so nobody decides it; the new head asks again." />
          </div>
          <div className="acd-stack">
            {superseded.map((a) => (
              <ApprovalCard key={a.id} a={a} />
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- approval */

type AState = 'pending' | 'approved' | 'denied' | 'expired' | 'superseded' | 'viewer'

const DECIDED: Record<AState, { word: string; tone: Tone } | null> = {
  pending: null,
  viewer: null,
  approved: { word: 'Approved', tone: 'ok' },
  denied: { word: 'Denied', tone: 'bad' },
  expired: { word: 'Expired', tone: 'paused' },
  superseded: { word: 'Superseded', tone: 'paused' },
}

function ApprovalFrame({ st }: { st: AState }) {
  const d = DECIDED[st]
  const [deny, setDeny] = useState(false)
  return (
    <div className="acd-page">
      <Breadcrumb crumbs={[{ key: 'a', label: 'Approvals', href: '#' }, { key: 'n', label: 'Merge PR #482', href: null }]} />
      <section className={`acd-head${st === 'pending' ? ' is-info' : ''}`} aria-label="Merge PR #482">
        <div className="acd-head-t">
          <div className="acd-card-h">
            <Tag>Merge</Tag>
            <h2>PR #482 &middot; Retry the release upload once</h2>
          </div>
          <dl className="acd-facts is-row">
            <div className="acd-pair"><dt>Repository</dt><dd>example-org/release-tools</dd></div>
            <div className="acd-pair"><dt>From</dt><dd>Failure review &middot; <span className="acd-mono">fir_20261019_0900</span></dd></div>
            <div className="acd-pair"><dt>Asked</dt><dd>Oct 19 09:14 UTC</dd></div>
          </dl>
        </div>
        {(st === 'pending' || st === 'viewer') && (
          <div className="acd-expiry">
            <span className="acd-k">Expires</span>
            <span className="acd-big">in 2 d 21 h</span>
            <span className="acd-dim">Oct 22 09:14 UTC</span>
          </div>
        )}
      </section>
      <div className="acd-cols is-2">
        <section className="acd-panel" aria-label="At its head">
          <div className="acd-panel-h"><h2>At its head</h2></div>
          <div className="acd-checks">
            <span className="acd-huge is-ok">10</span>
            <span>
              <span className="acd-strong">of 10 checks passed</span>
              <br />
              <span className="acd-dim acd-mono">head 3e7a91c</span>
            </span>
          </div>
          <span className="acd-bar is-ok" aria-hidden><i style={{ width: '100%' }} /></span>
          <dl className="acd-facts">
            <dt>Files</dt>
            <dd>4 changed &middot; +38 &minus;11</dd>
            <dt>Mergeable</dt>
            <dd>yes</dd>
          </dl>
          {st === 'superseded' && (
            <div className="acd-check is-warn">
              <Mark tone="warn" />
              <span className="acd-strong">Head moved to <span className="acd-mono">b04c2d8</span></span>
            </div>
          )}
        </section>
        <section className="acd-panel" aria-label="Why it is held">
          <div className="acd-panel-h">
            <h2>Held for the owner</h2>
            <Info about="Held" tip="A merge that touches a protected path is held for the approvers named by the schedule's merge gate, even when every check passed." />
          </div>
          <ul className="acd-paths">
            <li><span className="acd-mono">.github/workflows/release.yml</span><span className="acd-dim">workflow file</span></li>
            <li><span className="acd-mono">scripts/lib/upload.sh</span><span className="acd-dim">shared script</span></li>
          </ul>
          <dl className="acd-facts">
            <dt>Approvers</dt>
            <dd className="acd-strong">owner only</dd>
            <dt>Owner</dt>
            <dd>{st === 'viewer' ? 'Avery Example' : 'Alice'}</dd>
          </dl>
        </section>
      </div>
      <section className={`acd-decide${d === null ? '' : ` is-${d.tone}`}`} aria-label="Decision">
        {st === 'pending' && !deny && (
          <>
            <Button kind="primary" size="lg">Approve and merge</Button>
            <Button kind="danger" size="lg" onClick={() => setDeny(true)}>Deny</Button>
            <span className="acd-grow" />
            <span className="acd-dim">head <span className="acd-mono">3e7a91c</span></span>
            <Info about="Head" tip="Approve merges head 3e7a91c only. A new commit asks again." />
          </>
        )}
        {st === 'pending' && deny && (
          <>
            <input className="acd-in acd-reason" placeholder="Reason" aria-label="Reason" />
            <Button kind="danger-filled">Deny</Button>
            <Button kind="ghost" onClick={() => setDeny(false)}>Cancel</Button>
          </>
        )}
        {st === 'viewer' && (
          <>
            <Mark tone="paused" />
            <span className="acd-strong">Owner only</span>
            <span className="acd-dim">Avery Example decides</span>
            <Info about="Who decides" tip="This schedule's merge gate names its owner, Avery Example, as the only approver. You can read the item; the decision is theirs." />
          </>
        )}
        {st === 'approved' && (
          <>
            <Mark tone="ok" />
            <span className="acd-strong">Approved</span>
            <span>by Alice &middot; Oct 19 10:02 UTC</span>
            <span className="acd-dim">merged at <span className="acd-mono">3e7a91c</span></span>
          </>
        )}
        {st === 'denied' && (
          <>
            <Mark tone="bad" />
            <span className="acd-strong">Denied</span>
            <span>by Alice &middot; Oct 19 10:05 UTC</span>
            <span className="acd-quote">&ldquo;Keep the workflow change in its own PR.&rdquo;</span>
          </>
        )}
        {st === 'expired' && (
          <>
            <Mark tone="paused" />
            <span className="acd-strong">Expired</span>
            <span>Oct 22 09:14 UTC &middot; nobody decided</span>
            <span className="acd-grow" />
            <Button>Open the schedule</Button>
          </>
        )}
        {st === 'superseded' && (
          <>
            <Mark tone="paused" />
            <span className="acd-strong">Superseded</span>
            <span>Oct 19 11:40 UTC &middot; the head moved</span>
            <span className="acd-grow" />
            <Button kind="primary">Open the new approval</Button>
          </>
        )}
      </section>
    </div>
  )
}

/* --------------------------------------------------------------- empties */

function EmptyNone() {
  return (
    <div className="acd-page">
      <EmptyState kind="empty" heading="No schedules" action={<Button kind="primary">New schedule</Button>}>
        <span className="acd-dim">Start from a type</span> <Info about="Tier" tip={TIER_TIP} />
      </EmptyState>
      <div className="acd-grid">
        {TEMPLATES.slice(0, 4).map((t) => (
          <section key={t.name} className="acd-card" aria-label={t.name}>
            <div className="acd-card-h"><span title={TIER_WORDS[t.tier]}><Tag>{t.tier}</Tag></span><h3 className="acd-mono">{t.name}</h3></div>
            <div className="acd-card-f"><Button size="sm">Use</Button></div>
          </section>
        ))}
      </div>
    </div>
  )
}

function EmptyNotServed() {
  return (
    <div className="acd-page">
      <EmptyState kind="failed" heading="Automate is not served here">
        <span className="acd-mono">GET /v1/schedules</span> answered 404 <Info about="Not served" tip="This deployment's API has no schedule routes. Schedules and approvals appear once it serves them." />
      </EmptyState>
    </div>
  )
}

/* ------------------------------------------------------------------ root */

export function AutomateCards({ frame }: { frame: string }) {
  switch (frame) {
    case 'schedule':
      return <ScheduleFrame />
    case 'edit':
      return <EditFrame />
    case 'approvals':
      return <ApprovalsFrame />
    case 'approval-pending':
    case 'approval-approved':
    case 'approval-denied':
    case 'approval-expired':
    case 'approval-superseded':
    case 'approval-viewer':
      return <ApprovalFrame st={frame.slice('approval-'.length) as AState} />
    case 'empty-none':
      return <EmptyNone />
    case 'empty-not-served':
      return <EmptyNotServed />
    case 'empty-unreadable':
      return <Landing unread />
    case 'empty-nothing-waiting':
      return <ApprovalsFrame none />
    default:
      return <Landing />
  }
}
