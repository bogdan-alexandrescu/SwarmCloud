// MOCK-HOOK FILE
/*
 * Automate redesign, direction "Time axis" (mock-up, ?automate=timeline).
 * Every schedule is one lane on a time axis: past firings to the left of now,
 * next firings to the right. An approval sits on the lane at the firing it
 * blocks, with a tail to its expiry. The list is one switch away (Axis | List).
 * Fixture data only: invented workspaces, schedules, ids, pull requests, people.
 * ?tl=landing|schedule|edit|approvals|approval|empty, &st=<state>.
 */
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { Button, Segmented, Tag } from './components'
import { PageHead } from './Shell'
import { InfoTip } from './People'
import { noteOwnName } from './Spine'
import { noteFixtureProbe, route } from './fetch'
import './styles/automate-timeline.css'

/* ------------------------------------------------------------------ clock */

const NOW = Date.parse('2026-10-19T12:00:00Z') // FIXTURE_CLOCK, a Monday
const H = 3_600_000
const D = 24 * H
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const p2 = (n: number) => String(n).padStart(2, '0')
const hm = (t: number) => { const d = new Date(t); return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}` }
const day = (t: number) => { const d = new Date(t); return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` }
const when = (t: number) => `${day(t)} ${hm(t)}`
function rel(t: number): string {
  const s = Math.round((t - NOW) / 60_000)
  const a = Math.abs(s)
  const v = a < 60 ? `${a} min` : a < 48 * 60 ? `${Math.round(a / 60)} h` : `${Math.round(a / 1440)} d`
  return s >= 0 ? `in ${v}` : `${v} ago`
}
const usd = (n: number) => `$${n.toFixed(2)}`

/* --------------------------------------------------------------- fixtures */

type Outcome = 'succeeded' | 'failed' | 'skipped' | 'partial' | 'refused'
type StateWord = 'active' | 'paused' | 'paused_by_tripwire' | 'waiting_on_approval' | 'failing' | 'never_fired'
type Approvers = 'members' | 'owner_only' | string[]
type Firing = {
  firing_id: string
  slot: number
  trigger: 'cron' | 'run_now' | 'catch_up'
  state: 'done' | 'skipped' | 'awaiting_approval' | 'created'
  outcome: Outcome | null
  cost_usd: number | null
  work: string
  approval_id?: string
  skip?: string
  ended_min?: number
}
type Sched = {
  schedule_id: string
  name: string
  type: string
  template: string
  target: string
  cron: string
  words: string
  timezone: string
  tier: string
  state_word: StateWord
  pause?: { by: string; at: number; reason: string; code?: string }
  gate: { run: 'auto' | 'approve'; plan: 'auto' | 'approve'; merge: 'auto' | 'approve' | 'off'; approvers: Approvers; approval_ttl_hours: number }
  budget: { per_run_usd: number; per_day_usd: number; max_concurrent: number }
  spend_today: { reported_usd: number; unreported_attempts: number; live_usd: number } | null
  consecutive_failures: number
  pending_approvals: number
  owner: string
  does: string[]
  every: { at: [number, number]; days?: number[]; hours?: number }
  firings: Firing[]
}

/** Slots of a cadence between two instants. */
function slots(e: Sched['every'], from: number, to: number): number[] {
  const out: number[] = []
  if (e.hours !== undefined) {
    const step = e.hours * H
    for (let t = Math.ceil((from - e.at[1] * 60_000) / step) * step + e.at[1] * 60_000; t <= to; t += step) out.push(t)
    return out
  }
  const d0 = Math.floor(from / D) * D
  for (let d = d0; d <= to; d += D) {
    const t = d + e.at[0] * H + e.at[1] * 60_000
    if (t < from || t > to) continue
    if (e.days !== undefined && !e.days.includes(new Date(t).getUTCDay())) continue
    out.push(t)
  }
  return out
}

let seq = 4100
function history(e: Sched['every'], back: number, pick: (i: number, n: number) => Partial<Firing>, upTo = NOW): Firing[] {
  const ts = slots(e, NOW - back * D, upTo)
  return ts.map((slot, i) => ({
    firing_id: `fir_${(seq++).toString(36)}q${i}`,
    slot,
    trigger: 'cron',
    state: 'done',
    outcome: 'succeeded',
    cost_usd: 0.4 + ((i * 37) % 9) / 10,
    work: 'task',
    ended_min: 6 + ((i * 13) % 20),
    ...pick(i, ts.length),
  }))
}

const OWNER = 'Robin Hale'
/** The fixture session's address (api.ts fixtures); its saved name is set per frame. */
const SIGNED_IN = 'alice@example.com'
const P = { owner: OWNER, sam: 'Sam Ortiz', kit: 'Kit Moreno', ana: 'Ana Lund' }

const MERGE_ID = 'apr_m7k2q9'
const PLAN_ID = 'apr_p3x8d1'

function makeSchedules(): Sched[] {
  const fr: Sched = {
    schedule_id: 'sch_fr41c', name: 'Failure review', type: 'failure-review', template: 'Review failed runs, fix what repeats',
    target: 'example-org/example-api', cron: '30 11 * * *', words: 'Every day at 11:30', timezone: 'UTC', tier: 'R2',
    state_word: 'waiting_on_approval',
    gate: { run: 'auto', plan: 'auto', merge: 'approve', approvers: 'owner_only', approval_ttl_hours: 72 },
    budget: { per_run_usd: 4, per_day_usd: 10, max_concurrent: 1 },
    spend_today: { reported_usd: 2.4, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 1, owner: OWNER,
    does: ['Reads the last 24 h of failed runs', 'Groups repeats by cause', 'Opens one fix pull request per cause'],
    every: { at: [11, 30] },
    firings: [],
  }
  fr.firings = history(fr.every, 14, (i, n) => (i === n - 1
    ? { state: 'awaiting_approval', outcome: null, approval_id: MERGE_ID, work: 'pull request #412', cost_usd: 2.4, ended_min: undefined }
    : i === n - 4 ? { outcome: 'partial', work: '2 of 3 causes' } : i === n - 6 ? { outcome: 'skipped', state: 'skipped', skip: 'OVERLAP', cost_usd: 0, work: '—' } : { work: i % 3 === 0 ? 'pull request' : 'no repeats' }))

  const dep: Sched = {
    schedule_id: 'sch_dp92e', name: 'Dependency refresh', type: 'dependency-cve-refresh', template: 'Bump vulnerable dependencies',
    target: 'example-org/example-web', cron: '0 2 * * *', words: 'Every day at 02:00', timezone: 'UTC', tier: 'R1',
    state_word: 'active',
    gate: { run: 'auto', plan: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 3, per_day_usd: 6, max_concurrent: 1 },
    spend_today: { reported_usd: 1.1, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 0, owner: P.sam,
    does: ['Checks advisories for every manifest', 'Bumps what is vulnerable', 'Merges when checks pass'],
    every: { at: [2, 0] }, firings: [],
  }
  dep.firings = history(dep.every, 14, (i) => (i === 5 ? { outcome: 'failed', work: 'workflow wf_7c1a' } : { work: 'workflow' }))

  const rel_: Sched = {
    schedule_id: 'sch_rl07b', name: 'Release health', type: 'release-health', template: 'Plan the week’s release',
    target: 'example-org/example-api', cron: '0 9 * * 1-5', words: 'Weekdays at 09:00', timezone: 'UTC', tier: 'R1',
    state_word: 'waiting_on_approval',
    gate: { run: 'auto', plan: 'approve', merge: 'off', approvers: 'members', approval_ttl_hours: 24 },
    budget: { per_run_usd: 2, per_day_usd: 4, max_concurrent: 1 },
    spend_today: { reported_usd: 0.3, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 1, owner: P.kit,
    does: ['Reads open release blockers', 'Drafts a release plan', 'Waits for a member to approve the plan'],
    every: { at: [9, 0], days: [1, 2, 3, 4, 5] }, firings: [],
  }
  rel_.firings = history(rel_.every, 14, (i, n) => (i === n - 1
    ? { state: 'awaiting_approval', outcome: null, approval_id: PLAN_ID, work: 'plan', cost_usd: 0.3, ended_min: undefined }
    : i === n - 3 ? { outcome: 'skipped', state: 'skipped', skip: 'APPROVAL_EXPIRED', cost_usd: 0.2, work: 'plan expired' } : { work: 'plan approved' }))

  const flake: Sched = {
    schedule_id: 'sch_fl33k', name: 'Flaky test hunt', type: 'ci-flake-hunter', template: 'Rerun and quarantine flaky tests',
    target: 'example-org/example-web', cron: '0 */4 * * *', words: 'Every 4 hours', timezone: 'UTC', tier: 'R1',
    state_word: 'paused_by_tripwire',
    pause: { by: 'tripwire', at: NOW - 2.5 * H, reason: 'Spend passed the day’s budget', code: 'RUN_OVER_BUDGET' },
    gate: { run: 'auto', plan: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 2, per_day_usd: 6, max_concurrent: 1 },
    spend_today: { reported_usd: 6.4, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 0, owner: P.ana,
    does: ['Reruns tests that failed once', 'Quarantines the ones that flip', 'Opens one issue per flaky test'],
    every: { at: [0, 0], hours: 4 }, firings: [],
  }
  flake.firings = history(flake.every, 14, (i, n) => (i >= n - 3 ? { cost_usd: 2.1 + (i % 2) * 0.1, work: 'quarantined 1' } : { work: 'nothing flaky' }), NOW - 2.5 * H)

  const plan: Sched = {
    schedule_id: 'sch_ip58m', name: 'Plan open issues', type: 'issue-plan-only', template: 'Plan, do not run',
    target: 'example-org/example-api', cron: '0 6 * * 1-5', words: 'Weekdays at 06:00', timezone: 'UTC', tier: 'R1',
    state_word: 'failing',
    gate: { run: 'auto', plan: 'auto', merge: 'off', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 1, per_day_usd: 3, max_concurrent: 2 },
    spend_today: { reported_usd: 0.9, unreported_attempts: 1, live_usd: 0 },
    consecutive_failures: 3, pending_approvals: 0, owner: P.kit,
    does: ['Reads issues labelled ready', 'Writes a plan on each', 'Never starts a run'],
    every: { at: [6, 0], days: [1, 2, 3, 4, 5] }, firings: [],
  }
  plan.firings = history(plan.every, 14, (i, n) => (i >= n - 3 ? { outcome: 'failed', work: 'repository unreachable', cost_usd: 0.3 } : { work: '4 plans' }))

  const cost: Sched = {
    schedule_id: 'sch_cr12w', name: 'Weekly cost report', type: 'cost-report', template: 'Spend by workspace and runner',
    target: 'every registered repository', cron: '0 8 * * 1', words: 'Mondays at 08:00', timezone: 'UTC', tier: 'R0',
    state_word: 'paused',
    pause: { by: P.sam, at: NOW - 3 * D, reason: 'Holiday week' },
    gate: { run: 'auto', plan: 'auto', merge: 'off', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 0.5, per_day_usd: 0.5, max_concurrent: 1 },
    spend_today: { reported_usd: 0, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 0, owner: P.sam,
    does: ['Totals last week’s spend', 'Posts it to the workspace'],
    every: { at: [8, 0], days: [1] }, firings: [],
  }
  cost.firings = history(cost.every, 28, () => ({ work: 'report', cost_usd: 0.1 }), NOW - 3 * D)

  const docs: Sched = {
    schedule_id: 'sch_dd70n', name: 'Docs drift check', type: 'docs-drift', template: 'Flag docs that disagree with code',
    target: 'example-org/example-docs', cron: '0 9 * * 2', words: 'Tuesdays at 09:00', timezone: 'UTC', tier: 'R0',
    state_word: 'never_fired',
    gate: { run: 'auto', plan: 'auto', merge: 'off', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 1, per_day_usd: 1, max_concurrent: 1 },
    spend_today: { reported_usd: 0, unreported_attempts: 0, live_usd: 0 },
    consecutive_failures: 0, pending_approvals: 0, owner: OWNER,
    does: ['Compares docs pages with the code they cite', 'Opens one issue per drift'],
    every: { at: [9, 0], days: [2] }, firings: [],
  }
  return [fr, rel_, flake, plan, dep, cost, docs]
}

type ApprovalState = 'pending' | 'approved' | 'rejected' | 'expired' | 'superseded'
type Appr = {
  approval_id: string
  kind: 'merge' | 'run' | 'plan' | 'proposal' | 'hold'
  title: string
  summary: string
  state: ApprovalState
  requested_at: number
  expires_at: number
  approvers: Approvers
  requested_by: string
  schedule_id?: string
  firing_id?: string
  decided_by?: string
  decided_at?: number
  reason?: string
}

function makeApprovals(s: Sched[]): Appr[] {
  const fr = s[0]!
  const rl = s[1]!
  const frF = fr.firings[fr.firings.length - 1]!
  const rlF = rl.firings[rl.firings.length - 1]!
  return [
    { approval_id: PLAN_ID, kind: 'plan', title: 'Release plan, week 43', summary: '6 blockers, 2 to ship', state: 'pending', requested_at: rlF.slot + 9 * 60_000, expires_at: rlF.slot + 9 * 60_000 + 24 * H, approvers: 'members', requested_by: `${rl.name} · ${rlF.firing_id}`, schedule_id: rl.schedule_id, firing_id: rlF.firing_id },
    { approval_id: 'apr_q1v6t2', kind: 'proposal', title: 'Raise the default runner to medium', summary: 'From workflow wf_5d2e', state: 'pending', requested_at: NOW - 2 * D - 3 * H, expires_at: NOW + 5 * D - 3 * H, approvers: 'members', requested_by: `${P.ana} · workflow wf_5d2e` },
    { approval_id: MERGE_ID, kind: 'merge', title: 'Merge pull request #412', summary: 'Retry the flaky upload step', state: 'pending', requested_at: frF.slot + 11 * 60_000, expires_at: frF.slot + 11 * 60_000 + 72 * H, approvers: 'owner_only', requested_by: `${fr.name} · ${frF.firing_id}`, schedule_id: fr.schedule_id, firing_id: frF.firing_id },
    { approval_id: 'apr_k8r3m5', kind: 'merge', title: 'Merge pull request #409', summary: 'Pin the cache action', state: 'approved', requested_at: NOW - 2 * D - 1 * H, expires_at: NOW + 1 * D, approvers: 'owner_only', requested_by: `${fr.name} · fir_3k2q11`, schedule_id: fr.schedule_id, decided_by: OWNER, decided_at: NOW - 2 * D + 2 * H },
    { approval_id: 'apr_d4n7w8', kind: 'run', title: 'Run issue #388', summary: 'Rewrite the export job', state: 'rejected', requested_at: NOW - 3 * D, expires_at: NOW, approvers: 'members', requested_by: `${P.kit} · issue run ir_7pq2`, decided_by: P.sam, decided_at: NOW - 3 * D + 4 * H, reason: 'Too wide; split it first' },
    { approval_id: 'apr_x5c9h0', kind: 'plan', title: 'Release plan, week 42', summary: '4 blockers, 1 to ship', state: 'expired', requested_at: NOW - 3 * D - 3 * H + 9 * 60_000, expires_at: NOW - 2 * D - 3 * H + 9 * 60_000, approvers: 'members', requested_by: `${rl.name} · fir_2x0q7`, schedule_id: rl.schedule_id },
    { approval_id: 'apr_s2b6f4', kind: 'merge', title: 'Merge pull request #405', summary: 'Retry the flaky upload step', state: 'superseded', requested_at: NOW - 4 * D, expires_at: NOW - 1 * D, approvers: 'owner_only', requested_by: `${fr.name} · fir_1z9q4`, schedule_id: fr.schedule_id },
    { approval_id: 'apr_h6t1y3', kind: 'run', title: 'Run issue #377', summary: 'Add retries to the mailer', state: 'approved', requested_at: NOW - 4 * D - 6 * H, expires_at: NOW - 1 * D, approvers: 'members', requested_by: `${P.ana} · issue run ir_3m8v`, decided_by: P.kit, decided_at: NOW - 4 * D - 5 * H },
  ]
}

/** The worked example: the merge approval's detail (GET /v1/approvals/{id} `merge`). */
const CHECKS = ['lint', 'unit (py 3.11)', 'unit (web)', 'types', 'build image', 'contract', 'shellcheck', 'terraform fmt', 'publish scan', 'e2e smoke']
const HELD_FILES = ['.github/workflows/ci.yml', 'scripts/lib/common.sh']

/* ------------------------------------------------------------------ glyphs */

const OUTCOME_WORD: Record<Outcome, string> = { succeeded: 'Succeeded', failed: 'Failed', skipped: 'Skipped', partial: 'Partly done', refused: 'Refused' }
/** `skip_reason` codes as words: the raw enum is never drawn. */
const SKIP_WORD: Record<string, string> = { OVERLAP: 'overlap', APPROVAL_EXPIRED: 'approval expired' }
const skipWord = (code: string) => SKIP_WORD[code] ?? code.toLowerCase().replace(/_/g, ' ')
function Glyph({ o, size = 12 }: { o: Outcome | 'next' | 'waits' | 'superseded' | 'trip'; size?: number }) {
  const s = size
  const c = s / 2
  const common = { width: s, height: s, viewBox: `0 0 ${s} ${s}`, 'aria-hidden': true as const, className: `atl-g is-${o}` }
  switch (o) {
    case 'succeeded': return <svg {...common}><circle cx={c} cy={c} r={c - 1.5} /></svg>
    case 'failed': return <svg {...common}><path d={`M2 2L${s - 2} ${s - 2}M${s - 2} 2L2 ${s - 2}`} /></svg>
    case 'skipped': return <svg {...common}><circle cx={c} cy={c} r={c - 2} /></svg>
    case 'partial': return <svg {...common}><circle cx={c} cy={c} r={c - 1.5} /><path d={`M${c} 1.5A${c - 1.5} ${c - 1.5} 0 0 1 ${c} ${s - 1.5}Z`} className="fill" /></svg>
    case 'refused': return <svg {...common}><circle cx={c} cy={c} r={c - 1.5} /><path d={`M3 ${s - 3}L${s - 3} 3`} /></svg>
    case 'next': return <svg {...common}><circle cx={c} cy={c} r={c - 2} /></svg>
    case 'waits': return <svg {...common}><path d={`M${c} 1L${s - 1} ${c}L${c} ${s - 1}L1 ${c}Z`} /></svg>
    case 'superseded': return <svg {...common}><path d={`M${c} 1.5L${s - 1.5} ${c}L${c} ${s - 1.5}L1.5 ${c}Z`} /></svg>
    case 'trip': return <svg {...common}><path d={`M${c} 1L${s - 1} ${s - 1.5}H1Z`} /></svg>
  }
}

/* -------------------------------------------------------------------- axis */

type Win = { from: number; to: number }
const pct = (w: Win, t: number) => ((t - w.from) / (w.to - w.from)) * 100
const inWin = (w: Win, t: number) => pct(w, t) > 0.5 && pct(w, t) < 99.5
/** A marker's left edge, kept whole inside the lane at either end of the window. */
const at = (w: Win, t: number) => `clamp(8px, ${pct(w, t)}%, calc(100% - 8px))`

const isPhone = () => typeof window !== 'undefined' && window.matchMedia('(max-width: 760px)').matches

function AxisHead({ w, compact: c = false, now = NOW }: { w: Win; compact?: boolean; now?: number }) {
  const compact = c || isPhone()
  const ticks: number[] = []
  for (let t = Math.ceil(w.from / D) * D; t <= w.to; t += D) ticks.push(t)
  const every = compact ? Math.max(1, Math.ceil(ticks.length / 4)) : ticks.length > 16 ? 7 : 1
  return (
    <div className="atl-axhead" aria-hidden="true">
      {ticks.map((t, i) => (i % every === 0 && !(pct(w, t) > (compact ? 86 : 94)) ? (
        <span key={t} className={`atl-tick${new Date(t).getUTCDay() === 1 ? ' is-mon' : ''}`} style={{ left: `${pct(w, t)}%` }}>
          {compact ? `${DAYS[new Date(t).getUTCDay()]} ${new Date(t).getUTCDate()}` : day(t)}
        </span>
      ) : null))}
      <span className={`atl-nowlab${pct(w, now) < 8 ? ' is-start' : ''}`} style={{ left: `${pct(w, now)}%` }}>now · {hm(now)}</span>
    </div>
  )
}

function Grid({ w, now = NOW }: { w: Win; now?: number }) {
  const ticks: number[] = []
  for (let t = Math.ceil(w.from / D) * D; t <= w.to; t += D) ticks.push(t)
  return (
    <>
      {ticks.map((t) => <span key={t} className="atl-gl" style={{ left: `${pct(w, t)}%` }} aria-hidden="true" />)}
      <span className="atl-now" style={{ left: `${pct(w, now)}%` }} aria-hidden="true" />
    </>
  )
}

/** What the open approval is called where it costs a firing. */
const askName = (a: Appr) => (a.kind === 'merge' ? a.title.replace('Merge pull request ', '') : `the ${a.kind}`)
/** Overlap is skip: every next firing before the open approval's expiry is skipped while it waits. */
const atRisk = (pending: Appr | undefined, t: number) => pending !== undefined && t < pending.expires_at

/** One schedule's lane: firings, next firings, a pause band, an approval at the firing it blocks. */
function Lane({ s, w, approvals, open, big = false, unread = false, now = NOW }: { s: Sched; w: Win; approvals: Appr[]; open: (a: Appr) => void; big?: boolean; unread?: boolean; now?: number }) {
  const paused = s.state_word === 'paused' || s.state_word === 'paused_by_tripwire'
  const nexts = paused ? [] : slots(s.every, now + 60_000, w.to).filter((t) => inWin(w, t))
  const pending = approvals.find((a) => a.schedule_id === s.schedule_id && a.state === 'pending')
  const pf = pending && s.firings.find((f) => f.approval_id === pending.approval_id)
  const asks = pending !== undefined && pf !== undefined && inWin(w, pf.slot)
  const thin = s.every.hours !== undefined && (w.to - w.from) > 8 * D
  return (
    <div className={`atl-lane${big ? ' is-big' : ''}${asks ? ' has-ask' : ''}`}>
      <Grid w={w} now={now} />
      {paused && s.pause && (
        <span className={`atl-band${s.state_word === 'paused_by_tripwire' ? ' is-trip' : ''}`} style={{ left: `${Math.max(0, pct(w, s.pause.at))}%`, right: 0 }}
          title={s.state_word === 'paused_by_tripwire' ? `Paused by its spend tripwire at ${when(s.pause.at)}` : `Paused by ${s.pause.by} at ${when(s.pause.at)}: ${s.pause.reason}`} />
      )}
      {asks && (
        <span className="atl-hold" style={{ left: `${pct(w, pending.requested_at)}%`, width: `${Math.min(pct(w, pending.expires_at), 100) - pct(w, pending.requested_at)}%` }} aria-hidden="true" />
      )}
      {s.firings.filter((f) => inWin(w, f.slot)).map((f) => {
        if (f.outcome === null) {
          return f.state === 'awaiting_approval'
            ? <span key={f.firing_id} className="atl-mk" style={{ left: at(w, f.slot) }} title={`${when(f.slot)} · waiting on approval`}><Glyph o="waits" size={big ? 14 : 12} /></span>
            : <span key={f.firing_id} className="atl-mk" style={{ left: at(w, f.slot) }} title={`${when(f.slot)} · ${f.work}`}><Glyph o="superseded" size={big ? 14 : 12} /></span>
        }
        const o = f.outcome
        if (thin && o === 'succeeded') return <span key={f.firing_id} className="atl-dot is-thin" style={{ left: at(w, f.slot) }} title={`${when(f.slot)} · ${OUTCOME_WORD[o]}`} />
        return (
          <span key={f.firing_id} className="atl-mk" style={{ left: at(w, f.slot) }} title={`${when(f.slot)} · ${OUTCOME_WORD[o]}${f.skip ? ` (${skipWord(f.skip)})` : ''}${f.state === 'skipped' ? '' : ` · ${f.work}`}${f.cost_usd !== null ? ` · ${usd(f.cost_usd)}` : ''}`}>
            <Glyph o={o} size={big ? 14 : 12} />
          </span>
        )
      })}
      {s.state_word === 'paused_by_tripwire' && s.pause && <span className="atl-mk" style={{ left: at(w, s.pause.at) }} title={`Tripwire at ${usd(s.budget.per_day_usd)} a day`}><Glyph o="trip" size={big ? 14 : 12} /></span>}
      {nexts.map((t) => (
        <span key={t} className={`atl-mk is-next${atRisk(pending, t) ? ' is-atrisk' : ''}`} style={{ left: at(w, t) }} title={`${when(t)} · next firing${pending && atRisk(pending, t) ? ` · skipped while ${askName(pending)} waits (overlap: skip)` : ''}`}>
          <Glyph o="next" size={big ? 14 : 12} />
        </span>
      ))}
      {asks && (
        <button type="button" className={`atl-ask${pending.kind === 'merge' ? ' is-merge' : ''}${pct(w, pf.slot) > 55 && isPhone() ? ' is-flip' : ''}`} style={{ left: `${pct(w, pf.slot)}%` }} onClick={() => open(pending)}>
          <Glyph o="waits" size={12} />
          <span>{pending.kind === 'merge' ? `Merge ${askName(pending)} · ${rel(pending.expires_at).replace('in ', '')} left` : `Plan · ${rel(pending.expires_at).replace('in ', '')} left`}</span>
        </button>
      )}
      {unread && <span className="atl-unread" title="Firings read failed: 503 from /v1/schedules/{id}">firings —</span>}
    </div>
  )
}

/* ------------------------------------------------------------- small parts */

function approversWord(a: Approvers): string {
  return a === 'owner_only' ? 'Owner only' : a === 'members' ? 'Any member' : a.join(', ')
}

function Spend({ s, unread = false }: { s: Sched; unread?: boolean }) {
  if (unread || s.spend_today === null) return <span className="atl-spend is-unread"><span className="c-dash" role="img" aria-label="Spend read failed: 503">—</span><InfoTip about="spend" tip="Spend read failed: 503 from /v1/schedules/spend at 12:00. Not counted as zero." end /><span className="atl-dim"> / {usd(s.budget.per_day_usd)}</span></span>
  const used = s.spend_today.reported_usd + s.spend_today.live_usd
  const over = used >= s.budget.per_day_usd
  const p = Math.min(100, (used / s.budget.per_day_usd) * 100)
  return (
    <span className={`atl-spend${over ? ' is-over' : ''}`} title={`${usd(used)} of ${usd(s.budget.per_day_usd)} a day${s.spend_today.unreported_attempts ? ` · ${s.spend_today.unreported_attempts} attempt not reported yet` : ''}`}>
      <span className="atl-spend-n">{usd(used)}<span className="atl-dim"> / {usd(s.budget.per_day_usd)}</span>{s.spend_today.unreported_attempts > 0 && <span className="atl-dim">+</span>}</span>
      <span className="atl-bar"><span style={{ width: `${p}%` }} /></span>
    </span>
  )
}

/** The one word a lane shows, only where it asks for something. */
function Ask({ s, approvals, open }: { s: Sched; approvals: Appr[]; open: (a: Appr) => void }) {
  if (s.state_word === 'waiting_on_approval') {
    const a = approvals.find((x) => x.schedule_id === s.schedule_id && x.state === 'pending')
    return a ? <button type="button" className="atl-askword is-warn" onClick={() => open(a)}>Waiting on approval</button> : null
  }
  if (s.state_word === 'paused_by_tripwire') return <span className="atl-askword is-bad">Tripwire</span>
  if (s.state_word === 'failing') return <span className="atl-askword is-bad">Failing {s.consecutive_failures}×</span>
  return null
}

function Legend() {
  return (
    <div className="atl-legend" aria-label="Legend">
      <span><Glyph o="succeeded" />Succeeded</span>
      <span><Glyph o="partial" />Partly</span>
      <span><Glyph o="failed" />Failed</span>
      <span><Glyph o="skipped" />Skipped</span>
      <span><Glyph o="waits" />Waits for approval</span>
      <span><Glyph o="trip" />Tripwire</span>
      <span><Glyph o="next" />Next</span>
      <span><i className="atl-band-sw" />Paused</span>
    </div>
  )
}

/* ----------------------------------------------------------------- screens */

type View =
  | { tl: 'landing'; st?: 'unreadable' }
  | { tl: 'schedule'; id: string }
  | { tl: 'edit'; id: string }
  | { tl: 'approvals'; st?: 'nothing-waiting' }
  | { tl: 'approval'; id: string; st: string }
  | { tl: 'empty'; st: 'none' | 'not-served' }

function readView(tab: 'schedules' | 'approvals'): View {
  const q = new URLSearchParams(window.location.search)
  const tl = q.get('tl') ?? (tab === 'approvals' ? 'approvals' : 'landing')
  const st = q.get('st') ?? ''
  const id = q.get('id') ?? ''
  if (tl === 'schedule') return { tl, id: id || 'sch_fr41c' }
  if (tl === 'edit') return { tl, id: id || 'sch_fr41c' }
  if (tl === 'approvals') return st === 'nothing-waiting' ? { tl, st } : { tl }
  if (tl === 'approval') return { tl, id: id || MERGE_ID, st: st || 'pending' }
  if (tl === 'empty') return { tl, st: st === 'not-served' ? 'not-served' : 'none' }
  return st === 'unreadable' ? { tl: 'landing', st } : { tl: 'landing' }
}

export function AutomateTimeline({ tab }: { tab: 'schedules' | 'approvals' }) {
  const [view, setView] = useState<View>(() => readView(tab))
  const [schedules] = useState(makeSchedules)
  const [approvals] = useState(() => makeApprovals(schedules))
  const go = (v: View) => {
    const q = new URLSearchParams(window.location.search)
    q.set('tl', v.tl)
    if ('id' in v) q.set('id', v.id); else q.delete('id')
    if ('st' in v && v.st) q.set('st', v.st); else q.delete('st')
    window.history.replaceState(null, '', `${window.location.pathname}?${q.toString()}`)
    setView(v)
    window.scrollTo(0, 0)
  }
  const openA = (a: Appr) => go({ tl: 'approval', id: a.approval_id, st: a.state === 'rejected' ? 'denied' : a.state })
  const openS = (s: Sched) => go({ tl: 'schedule', id: s.schedule_id })
  const viewer = view.tl === 'approval' && view.st === 'viewer'
  const reads = view.tl === 'empty' ? view.st : view.tl === 'landing' ? view.st ?? '' : ''
  // The signed-in person: the owner, Robin Hale, everywhere but the viewer frame, where the
  // fixture's own member (a workspace admin who is not the schedule's owner) looks on.
  useEffect(() => { noteOwnName(SIGNED_IN, viewer ? null : OWNER) }, [viewer])
  // The Reads strip counts what this screen's reads did, so a failed read here is a failed read there.
  useEffect(() => {
    if (reads === 'not-served') noteFixtureProbe(route('/v1/schedules'), 90, false, 'not_found')
    if (reads === 'unreadable') {
      noteFixtureProbe(route('/v1/approvals'), 120, false, 'server_error')
      noteFixtureProbe(route('/v1/schedules/spend'), 140, false, 'server_error')
    }
  }, [reads])
  const onApprovals = view.tl === 'approvals' || view.tl === 'approval'
  return (
    <div className="atl" data-mock="automate-timeline">
      <div className="atl-phonetabs">
        <Segmented label="Automate" small value={onApprovals ? 'approvals' : 'schedules'} onChange={(k) => go(k === 'approvals' ? { tl: 'approvals' } : { tl: 'landing' })} options={[{ key: 'schedules', label: 'Schedules' }, { key: 'approvals', label: 'Approvals', count: approvals.filter((a) => a.state === 'pending').length }]} />
      </div>
      {view.tl === 'landing' && <Landing schedules={schedules} approvals={approvals} openA={openA} openS={openS} go={go} unread={view.st === 'unreadable'} />}
      {view.tl === 'schedule' && <ScheduleView s={schedules.find((x) => x.schedule_id === view.id) ?? schedules[0]!} approvals={approvals} openA={openA} go={go} />}
      {view.tl === 'edit' && <EditView s={schedules.find((x) => x.schedule_id === view.id) ?? schedules[0]!} go={go} />}
      {view.tl === 'approvals' && <ApprovalsView approvals={view.st === 'nothing-waiting' ? approvals.filter((a) => a.state !== 'pending') : approvals} openA={openA} schedules={schedules} />}
      {view.tl === 'approval' && <ApprovalView a={approvals.find((x) => x.approval_id === view.id) ?? approvals[2]!} st={view.st} schedules={schedules} go={go} />}
      {view.tl === 'empty' && <EmptyView st={view.st} go={go} />}
    </div>
  )
}

/* ---- landing ---- */

const RANGES = [
  { key: '1d', label: 'Day', back: 0.5 * D, fwd: 0.5 * D },
  { key: '7d', label: 'Week', back: 3.5 * D, fwd: 3.5 * D },
  { key: '30d', label: 'Month', back: 21 * D, fwd: 9 * D },
] as const
type RangeKey = (typeof RANGES)[number]['key']

function Landing({ schedules, approvals, openA, openS, go, unread }: { schedules: Sched[]; approvals: Appr[]; openA: (a: Appr) => void; openS: (s: Sched) => void; go: (v: View) => void; unread: boolean }) {
  const [range, setRange] = useState<RangeKey>('7d')
  const [mode, setMode] = useState<'axis' | 'list'>('axis')
  const r = RANGES.find((x) => x.key === range) ?? RANGES[1]
  const w: Win = { from: NOW - r.back, to: NOW + r.fwd }
  const waiting = approvals.filter((a) => a.state === 'pending').sort((a, b) => a.requested_at - b.requested_at)
  const loose = waiting.filter((a) => a.schedule_id === undefined)
  const order: StateWord[] = ['waiting_on_approval', 'paused_by_tripwire', 'failing', 'active', 'never_fired', 'paused']
  const sorted = [...schedules].sort((a, b) => order.indexOf(a.state_word) - order.indexOf(b.state_word))
  return (
    <>
      <PageHead title="Schedules" info={<InfoTip about="Schedules" tip="Each lane is one schedule. Left of now: past firings and their outcomes. Right of now: next firings. An approval sits on the firing it holds, its bar running to its expiry." />}>
        <Segmented label="View" small value={mode} onChange={setMode} options={[{ key: 'axis', label: 'Axis' }, { key: 'list', label: 'List' }]} />
        <Button kind="primary" onClick={() => go({ tl: 'edit', id: 'new' })}>New schedule</Button>
      </PageHead>
      <div className="atl-sum">
        <span className="atl-sum-i"><b>{schedules.length}</b> schedules</span>
        {unread
          ? <span className="atl-sum-i">waiting <span className="c-dash" role="img" aria-label="Approvals read failed">—</span><InfoTip about="waiting" tip="Approvals read failed: 503 from /v1/approvals at 12:00. Not counted as zero." /></span>
          : <button type="button" className="atl-sum-i is-ask" onClick={() => go({ tl: 'approvals' })}><b>{waiting.length}</b> waiting on approval</button>}
        {unread
          ? <span className="atl-sum-i">today <span className="c-dash" role="img" aria-label="Spend read failed">—</span></span>
          : <span className="atl-sum-i"><b>{usd(schedules.reduce((n, s) => n + (s.spend_today?.reported_usd ?? 0), 0))}</b> today</span>}
        <span className="atl-sp" />
        {mode === 'axis' && <Segmented label="Range" small value={range} onChange={setRange} options={RANGES.map((x) => ({ key: x.key, label: x.label }))} />}
      </div>
      {unread && (
        <div className="atl-warnline" role="status">
          <span><b>2 reads failed</b> · approvals, spend</span>
          <Button size="sm">Read again</Button>
        </div>
      )}
      {mode === 'list' ? <ListMode schedules={sorted} approvals={approvals} openS={openS} openA={openA} unread={unread} /> : (
        <section className="atl-board" aria-label="Schedules on a time axis">
          <div className="atl-row atl-headrow">
            <span className="atl-c-name">Schedule</span>
            <AxisHead w={w} />
            <span className="atl-c-spend">Today <InfoTip about="today's spend" tip="Reported spend plus live work, against the schedule’s per-day budget; at the budget the tripwire pauses it." end /></span>
          </div>
          {!unread && loose.length > 0 && (
            <div className="atl-row atl-looserow">
              <span className="atl-c-name"><span className="atl-n">Not from a schedule</span><span className="atl-sub">{loose.length} waiting</span></span>
              <div className="atl-lane">
                <Grid w={w} />
                {loose.map((a) => (
                  <Fragment key={a.approval_id}>
                    <span className="atl-hold" style={{ left: `${Math.max(0, pct(w, a.requested_at))}%`, width: `${Math.min(100, pct(w, a.expires_at)) - Math.max(0, pct(w, a.requested_at))}%` }} aria-hidden="true" />
                    <button type="button" className="atl-ask" style={{ left: `${Math.max(1, pct(w, a.requested_at))}%` }} onClick={() => openA(a)}><Glyph o="waits" /><span>Proposal · {rel(a.expires_at).replace('in ', '')} left</span></button>
                  </Fragment>
                ))}
              </div>
              <span className="atl-c-spend" />
            </div>
          )}
          {sorted.map((s) => (
            <div className={`atl-row is-${s.state_word}`} key={s.schedule_id}>
              <span className="atl-c-name">
                <a className="atl-n" href={`?automate=timeline&tl=schedule&id=${s.schedule_id}`} onClick={(e) => { e.preventDefault(); openS(s) }}>{s.name}</a>
                <span className="atl-sub" title={`${s.type} · ${s.target}`}>{s.type} · {s.target.replace('example-org/', '')}</span>
                <Ask s={s} approvals={approvals} open={openA} />
              </span>
              <Lane s={s} w={w} approvals={unread ? [] : approvals} open={openA} />
              <span className="atl-c-spend"><Spend s={s} unread={unread} /></span>
            </div>
          ))}
          <Legend />
        </section>
      )}
    </>
  )
}

function ListMode({ schedules, approvals, openS, openA, unread }: { schedules: Sched[]; approvals: Appr[]; openS: (s: Sched) => void; openA: (a: Appr) => void; unread: boolean }) {
  return (
    <div className="atl-tablewrap">
      <table className="atl-table">
        <thead><tr><th>Schedule</th><th>When</th><th>Last</th><th>Next</th><th>Today</th><th /></tr></thead>
        <tbody>
          {schedules.map((s) => {
            const last = s.firings[s.firings.length - 1]
            const paused = s.state_word.startsWith('paused')
            const next = paused ? null : slots(s.every, NOW + 60_000, NOW + 30 * D)[0] ?? null
            return (
              <tr key={s.schedule_id}>
                <td><a className="atl-n" href="#" onClick={(e) => { e.preventDefault(); openS(s) }}>{s.name}</a><span className="atl-sub" title={`${s.type} · ${s.target}`}>{s.type} · {s.target.replace('example-org/', '')}</span></td>
                <td>{s.words} <span className="atl-dim">{s.timezone}</span></td>
                <td>{last ? <span className="atl-inl"><Glyph o={last.outcome ?? 'waits'} />{rel(last.slot)}</span> : <span className="atl-dim">never</span>}</td>
                <td>{next === null ? <span className="atl-dim">paused</span> : rel(next)}</td>
                <td><Spend s={s} unread={unread} /></td>
                <td><Ask s={s} approvals={approvals} open={openA} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/* ---- schedule ---- */

function Facts({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return <dl className="atl-facts">{rows.map(([k, v], i) => <Fragment key={i}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>
}

function ScheduleView({ s, approvals, openA, go }: { s: Sched; approvals: Appr[]; openA: (a: Appr) => void; go: (v: View) => void }) {
  // Fourteen whole days, Sat 10 to Fri 23 inclusive, so the last day's firing and label are inside it.
  const w: Win = { from: NOW - 9.5 * D, to: NOW + 4.5 * D }
  const pending = approvals.find((a) => a.schedule_id === s.schedule_id && a.state === 'pending')
  const paused = s.state_word.startsWith('paused')
  const nexts = paused ? [] : slots(s.every, NOW + 60_000, NOW + 30 * D).slice(0, 5)
  const hist = [...s.firings].reverse().slice(0, 8)
  const sp = s.spend_today ?? { reported_usd: 0, live_usd: 0, unreported_attempts: 0 }
  const used = sp.reported_usd + sp.live_usd
  const counts = s.firings.reduce<Record<string, number>>((m, f) => { const k = f.outcome ?? 'waits'; m[k] = (m[k] ?? 0) + 1; return m }, {})
  return (
    <>
      <div className="atl-crumb"><Button kind="ghost" size="sm" onClick={() => go({ tl: 'landing' })}>Schedules</Button><span className="atl-dim">/</span><span>{s.name}</span></div>
      <PageHead title={s.name} info={<InfoTip about={s.name} tip={`${s.template}. Owned by ${s.owner}. Risk tier ${s.tier}.`} />}>
        <Button>Run now</Button>
        <Button>{paused ? 'Resume' : 'Pause'}</Button>
        <Button kind="primary" onClick={() => go({ tl: 'edit', id: s.schedule_id })}>Edit</Button>
      </PageHead>
      <div className="atl-idline">
        <Tag>{s.type}</Tag><span>{s.target}</span><span className="atl-dim">·</span><span>{s.words} {s.timezone}</span><span className="atl-dim">·</span><code>{s.cron}</code>
      </div>
      <section className="atl-card atl-axiscard" aria-label="Firings on a time axis">
        <div className="atl-card-h"><h2>Firings</h2>
          <span className="atl-counts">
            {(['succeeded', 'partial', 'failed', 'skipped'] as const).map((k) => <span key={k}><Glyph o={k} />{counts[k] ?? 0}</span>)}
            <span className="atl-dim">last 14 days</span>
          </span>
        </div>
        <div className="atl-bigaxis">
          <AxisHead w={w} />
          <Lane s={s} w={w} approvals={approvals} open={openA} big />
        </div>
        {pending && (
          <div className="atl-blocker">
            <Glyph o="waits" />
            <span><b>{pending.title}</b> holds the {hm(s.firings[s.firings.length - 1]!.slot)} firing · expires {when(pending.expires_at)} ({rel(pending.expires_at)}) · {approversWord(pending.approvers)}</span>
            <span className="atl-sp" />
            <Button size="sm" kind="primary" onClick={() => openA(pending)}>Open approval</Button>
          </div>
        )}
      </section>
      <div className="atl-grid3">
        <section className="atl-card">
          <div className="atl-card-h"><h2>What it does</h2></div>
          <ol className="atl-steps">{s.does.map((d) => <li key={d}>{d}</li>)}</ol>
          <Facts rows={[['Template', s.template], ['Type', s.type], ['Target', s.target], ['Overlap', 'Skip'], ['Catch-up', 'Skip']]} />
        </section>
        <section className="atl-card">
          <div className="atl-card-h"><h2>Next firings</h2></div>
          {nexts.length === 0 ? <p className="atl-dim">Paused · none</p> : (
            <ul className="atl-nexts">
              {nexts.map((t) => (
                <li key={t}><Glyph o="next" /><span className="atl-mono">{when(t)}</span><span className="atl-dim">{rel(t)}</span>
                  {pending && atRisk(pending, t) && <span className="atl-tagwarn" title={`Overlap is skip: this firing is skipped if the approval still waits; it expires ${when(pending.expires_at)}`}>skipped while {askName(pending)} waits</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="atl-card">
          <div className="atl-card-h"><h2>Spend today</h2><InfoTip about="tripwire" tip="At the per-day budget the tripwire pauses the schedule until the next UTC day or a resume." end /></div>
          <div className="atl-bigspend"><b>{usd(used)}</b><span className="atl-dim"> of {usd(s.budget.per_day_usd)}</span></div>
          <div className="atl-bar is-big"><span style={{ width: `${Math.min(100, (used / s.budget.per_day_usd) * 100)}%` }} /><i style={{ left: '100%' }} /></div>
          <Facts rows={[['Reported', usd(sp.reported_usd)], ['Live', usd(sp.live_usd)], ['Not reported yet', `${sp.unreported_attempts} attempts`], ['Per firing', `at most ${usd(s.budget.per_run_usd)}`], ['At once', `${s.budget.max_concurrent}`]]} />
        </section>
      </div>
      <div className="atl-grid2">
        <section className="atl-card">
          <div className="atl-card-h"><h2>Gates</h2></div>
          <div className="atl-gates">
            {(['run', 'plan', 'merge'] as const).map((g) => (
              <div key={g} className={`atl-gate is-${s.gate[g]}`}><span className="atl-dim">{g.charAt(0).toUpperCase() + g.slice(1)}</span><b>{s.gate[g] === 'approve' ? 'Needs approval' : s.gate[g] === 'off' ? 'Off' : 'Automatic'}</b></div>
            ))}
          </div>
          <Facts rows={[['Approvers', s.gate.approvers === 'owner_only' ? `Owner only · ${OWNER}` : approversWord(s.gate.approvers)], ['Expires after', `${s.gate.approval_ttl_hours} h`]]} />
        </section>
        <section className="atl-card atl-hist">
          <div className="atl-card-h"><h2>History</h2></div>
          <table className="atl-table is-tight">
            <thead><tr><th>Slot</th><th>Outcome</th><th>Work</th><th>Cost</th><th>Took</th></tr></thead>
            <tbody>
              {hist.map((f) => (
                <tr key={f.firing_id}>
                  <td className="atl-mono">{when(f.slot)}</td>
                  <td><span className="atl-inl"><Glyph o={f.outcome ?? 'waits'} />{f.outcome === null ? <button type="button" className="atl-linkbtn" onClick={() => pending && openA(pending)}>Waiting on approval</button> : f.outcome === 'succeeded' ? null : OUTCOME_WORD[f.outcome]}</span></td>
                  <td>{f.state === 'skipped' && f.skip ? skipWord(f.skip) : f.work}</td>
                  <td className="atl-mono" data-label="cost">{f.cost_usd === null ? '—' : usd(f.cost_usd)}</td>
                  <td className="atl-mono" data-label="took">{f.state === 'skipped' ? <span className="atl-dim">—</span> : f.ended_min === undefined ? <span className="atl-dim">open</span> : `${f.ended_min} min`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </>
  )
}

/* ---- edit ---- */

function EditView({ s, go }: { s: Sched; go: (v: View) => void }) {
  const [cad, setCad] = useState<'hourly' | 'daily' | 'weekdays' | 'weekly' | 'cron'>('weekdays')
  const draft: Sched['every'] = cad === 'hourly' ? { at: [0, 30], hours: 6 } : cad === 'daily' ? { at: [11, 30] } : cad === 'weekly' ? { at: [11, 30], days: [1] } : { at: [11, 30], days: [1, 2, 3, 4, 5] }
  const w: Win = { from: NOW, to: NOW + 14 * D }
  const nexts = slots(draft, NOW + 60_000, NOW + 30 * D).slice(0, 6)
  const preview: Sched = { ...s, state_word: 'active', firings: [], every: draft }
  const fresh = s.schedule_id === 'new'
  return (
    <>
      <div className="atl-crumb"><Button kind="ghost" size="sm" onClick={() => go({ tl: 'landing' })}>Schedules</Button><span className="atl-dim">/</span><span>{s.name}</span><span className="atl-dim">/</span><span>Edit</span></div>
      <PageHead title={fresh ? 'New schedule' : `Edit ${s.name}`}>
        <Button onClick={() => go({ tl: 'schedule', id: s.schedule_id })}>Cancel</Button>
        <Button kind="primary" disabledReason="Fix 1 field first">Save</Button>
      </PageHead>
      <div className="atl-editgrid">
        <form className="atl-form" onSubmit={(e) => e.preventDefault()}>
          <fieldset><legend>Type and template</legend>
            <div className="atl-row2">
              <label className="atl-fld" style={{ flex: 0.9 }}><span>Type</span><select defaultValue={s.type} disabled><option>{s.type}</option></select></label>
              <label className="atl-fld" style={{ flex: 2.4 }}><span>Template</span><select defaultValue="t1"><option value="t1">{s.template}</option><option>Review failed runs, report only</option></select></label>
            </div>
            <label className="atl-fld"><span>Name</span><input defaultValue={s.name} /></label>
          </fieldset>
          <fieldset><legend>Target</legend>
            <div className="atl-chips"><span className="atl-chip">example-org/example-api</span><span className="atl-chip">example-org/example-web</span><Button size="sm" kind="ghost">Add repository</Button></div>
          </fieldset>
          <fieldset><legend>Cadence</legend>
            <Segmented label="Cadence" small value={cad} onChange={setCad} options={[{ key: 'hourly', label: 'Hourly' }, { key: 'daily', label: 'Daily' }, { key: 'weekdays', label: 'Weekdays' }, { key: 'weekly', label: 'Weekly' }, { key: 'cron', label: 'Cron' }]} />
            <div className="atl-row2">
              <label className="atl-fld"><span>At</span><input defaultValue="11:30" className="atl-mono" /></label>
              <label className="atl-fld"><span>Time zone</span><select defaultValue="UTC"><option>UTC</option><option>Europe/Lisbon</option></select></label>
              <label className="atl-fld"><span>Cron</span><input readOnly value={cad === 'hourly' ? '30 */6 * * *' : cad === 'daily' ? '30 11 * * *' : cad === 'weekly' ? '30 11 * * 1' : '30 11 * * 1-5'} className="atl-mono" /></label>
            </div>
          </fieldset>
          <fieldset><legend>Gates <InfoTip about="gates" tip="Automatic acts without asking. Needs approval holds the firing until an approver decides or the request expires; expired skips it." /></legend>
            {(['run', 'plan', 'merge'] as const).map((g) => (
              <div className="atl-gaterow" key={g}><span>{g.charAt(0).toUpperCase() + g.slice(1)}</span>
                <Segmented label={`${g} gate`} small value={g === 'merge' ? 'approve' : 'auto'} options={g === 'merge' ? [{ key: 'auto', label: 'Automatic' }, { key: 'approve', label: 'Needs approval' }, { key: 'off', label: 'Off' }] : [{ key: 'auto', label: 'Automatic' }, { key: 'approve', label: 'Needs approval' }]} />
              </div>
            ))}
            <div className="atl-row2">
              <label className="atl-fld"><span>Approvers</span><select defaultValue="owner"><option value="owner">Owner only</option><option>Any member</option><option>Named people</option></select></label>
              <label className="atl-fld"><span>Expires after (h)</span><input defaultValue="72" className="atl-mono" /></label>
            </div>
          </fieldset>
          <fieldset><legend>Budget</legend>
            <div className="atl-row2">
              <label className="atl-fld is-bad"><span>Per firing ($)</span><input defaultValue="12.00" className="atl-mono" aria-invalid="true" aria-describedby="atl-e1" /><em id="atl-e1">At most $8.00 for {s.type}</em></label>
              <label className="atl-fld"><span>Per day ($)</span><input defaultValue="10.00" className="atl-mono" /></label>
              <label className="atl-fld"><span>At once</span><input defaultValue="1" className="atl-mono" /></label>
            </div>
          </fieldset>
        </form>
        <aside className="atl-card atl-preview" aria-label="Preview">
          <div className="atl-card-h"><h2>Next firings</h2><span className="atl-dim">next 14 days</span></div>
          <div className="atl-bigaxis is-pv"><AxisHead w={w} compact /><Lane s={preview} w={w} approvals={[]} open={() => undefined} /></div>
          <ul className="atl-nexts">{nexts.map((t) => <li key={t}><Glyph o="next" /><span className="atl-mono">{when(t)}</span><span className="atl-dim">{rel(t)}</span></li>)}</ul>
          <div className="atl-valid">
            <div className="is-bad"><Glyph o="failed" />Per firing over the type’s cap</div>
            <div className="is-ok"><Glyph o="succeeded" />Cron parses · {cad === 'weekdays' ? 'Weekdays at 11:30 UTC' : cad === 'daily' ? 'Every day at 11:30 UTC' : cad === 'weekly' ? 'Mondays at 11:30 UTC' : cad === 'hourly' ? 'Every 6 hours at :30' : 'Weekdays at 11:30 UTC'}</div>
            <div className="is-ok"><Glyph o="succeeded" />Owner may approve merges</div>
            <div className="is-ok"><Glyph o="succeeded" />Worst day {usd(10)} · within the workspace’s cap</div>
          </div>
        </aside>
      </div>
    </>
  )
}

/* ---- approvals ---- */

const KINDS = ['all', 'merge', 'plan', 'run', 'proposal'] as const
type KindKey = (typeof KINDS)[number]

function ExpiryBar({ a }: { a: Appr }) {
  const span = a.expires_at - a.requested_at
  const used = Math.min(100, Math.max(0, ((NOW - a.requested_at) / span) * 100))
  return (
    <span className={`atl-exp${used > 75 ? ' is-late' : ''}`} title={`Asked ${when(a.requested_at)} · expires ${when(a.expires_at)}`}>
      <span className="atl-exp-t"><span style={{ width: `${used}%` }} /></span>
      <span className="atl-exp-w">{rel(a.expires_at).replace('in ', '')} left</span>
    </span>
  )
}

function ApprovalsView({ approvals, openA, schedules }: { approvals: Appr[]; openA: (a: Appr) => void; schedules: Sched[] }) {
  const [kind, setKind] = useState<KindKey>('all')
  const by = (a: Appr) => kind === 'all' || a.kind === kind
  const waiting = approvals.filter((a) => a.state === 'pending' && by(a)).sort((a, b) => a.requested_at - b.requested_at)
  const decided = approvals.filter((a) => (a.state === 'approved' || a.state === 'rejected' || a.state === 'superseded') && by(a)).sort((a, b) => (b.decided_at ?? b.requested_at) - (a.decided_at ?? a.requested_at))
  const expired = approvals.filter((a) => a.state === 'expired' && by(a))
  const sname = (a: Appr) => schedules.find((s) => s.schedule_id === a.schedule_id)?.name
  const slotOf = (a: Appr) => schedules.find((s) => s.schedule_id === a.schedule_id)?.firings.find((f) => f.firing_id === a.firing_id)?.slot
  return (
    <>
      <PageHead title="Approvals" info={<InfoTip about="Approvals" tip="Requests that hold a firing, a merge, a plan or a proposal until an approver decides. An expired request skips what it held." />} />
      <div className="atl-sum">
        <Segmented label="Kind" small value={kind} onChange={setKind} options={KINDS.map((k) => ({ key: k, label: k === 'all' ? 'All' : k.charAt(0).toUpperCase() + k.slice(1), count: approvals.filter((a) => k === 'all' || a.kind === k).length }))} />
      </div>
      <section className="atl-card">
        <div className="atl-card-h"><h2>Waiting · {waiting.length}</h2><span className="atl-dim">oldest first</span></div>
        {waiting.length === 0 ? (
          <div className="atl-quiet"><Glyph o="succeeded" /><span>Nothing waiting</span><span className="atl-dim">read 12:00</span></div>
        ) : (
          <ul className="atl-alist">
            {waiting.map((a) => (
              <li key={a.approval_id}>
                <Tag>{a.kind}</Tag>
                <span className="atl-a-main"><a href="#" className="atl-n" onClick={(e) => { e.preventDefault(); openA(a) }}>{a.title}</a><span className="atl-sub">{sname(a) ? `${sname(a)} · holds the ${hm(slotOf(a) ?? a.requested_at)} firing` : a.requested_by} · {approversWord(a.approvers)}</span></span>
                <span className="atl-dim atl-mono">{rel(a.requested_at)}</span>
                <ExpiryBar a={a} />
                <span className="atl-a-acts">
                  {a.approvers === 'owner_only'
                    ? <Button size="sm" kind="primary" onClick={() => openA(a)}>Review</Button>
                    : <><Button size="sm">Deny</Button><Button size="sm" kind="primary">Approve</Button></>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <div className="atl-grid2 is-wide">
        <section className="atl-card">
          <div className="atl-card-h"><h2>Decided</h2><span className="atl-dim">last 7 days</span></div>
          <table className="atl-table is-tight">
            <thead><tr><th>Request</th><th>Decision</th><th>By</th><th>When</th></tr></thead>
            <tbody>{decided.map((a) => (
              <tr key={a.approval_id}>
                <td><a href="#" className="atl-n" onClick={(e) => { e.preventDefault(); openA(a) }}>{a.title}</a><span className="atl-sub">{a.kind} · {sname(a) ?? a.requested_by}</span></td>
                <td>{a.state === 'approved' ? 'Approved' : a.state === 'rejected' ? 'Denied' : 'Superseded'}{a.reason ? <span className="atl-sub">“{a.reason}”</span> : null}</td>
                <td>{a.decided_by ?? <span className="atl-dim">head moved</span>}</td>
                <td className="atl-mono">{when(a.decided_at ?? a.expires_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </section>
        <section className="atl-card">
          <div className="atl-card-h"><h2>Expired</h2></div>
          {expired.length === 0 ? <p className="atl-dim">None</p> : (
            <table className="atl-table is-tight">
              <thead><tr><th>Request</th><th>Expired</th><th>Held</th></tr></thead>
              <tbody>{expired.map((a) => (
                <tr key={a.approval_id}>
                  <td><a href="#" className="atl-n" onClick={(e) => { e.preventDefault(); openA(a) }}>{a.title}</a><span className="atl-sub">{a.kind} · {approversWord(a.approvers)}</span></td>
                  <td className="atl-mono">{when(a.expires_at)}</td>
                  <td>{sname(a) ? `${sname(a)} firing, skipped` : '—'}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </section>
      </div>
    </>
  )
}

/* ---- one approval ---- */

function ApprovalView({ a, st, schedules, go }: { a: Appr; st: string; schedules: Sched[]; go: (v: View) => void }) {
  const s = schedules.find((x) => x.schedule_id === a.schedule_id)
  const merge = a.kind === 'merge'
  const viewer = st === 'viewer'
  const state: string = viewer ? 'pending' : st
  const pr = a.title.replace('Merge pull request ', '')
  const firing = s?.firings.find((f) => f.approval_id === a.approval_id) ?? s?.firings[s.firings.length - 1]
  // Decided eleven minutes after it was asked, before the fixture's now.
  const decidedAt = Math.min(a.requested_at + 11 * 60_000, NOW)
  const head = { asked: '4c1e9a2', now: '8f03b7d' }
  // The axis as it stands in this state: an expired request is drawn after its expiry, with the
  // held firing and every firing it overlapped skipped; a decided or superseded one no longer waits.
  const now = state === 'expired' ? a.expires_at + 19 * 60_000 : NOW
  // Expired: from the held firing's day, so it and the firings it overlapped are all on the axis.
  const w: Win = state === 'expired' && firing ? { from: Math.floor(firing.slot / D) * D, to: Math.floor(firing.slot / D) * D + 5 * D } : { from: now - 2 * D, to: now + 4 * D }
  const axis: Sched | undefined = s && firing && state !== 'pending' ? {
    ...s,
    state_word: 'active',
    firings: [
      ...s.firings.map((f): Firing => (f.firing_id !== firing.firing_id ? f
        : state === 'approved' ? { ...f, state: 'done', outcome: 'succeeded', work: `merged ${pr} at ${head.asked}`, ended_min: 23 }
        : state === 'denied' ? { ...f, state: 'done', outcome: 'refused', work: `merge of ${pr} denied`, ended_min: 23 }
        : state === 'expired' ? { ...f, state: 'skipped', outcome: 'skipped', skip: 'APPROVAL_EXPIRED', ended_min: undefined }
        : { ...f, state: 'created', outcome: null, work: `request superseded, head moved · apr_r9w2e6 asks again` })),
      ...(state === 'expired' ? slots(s.every, firing.slot + 60_000, a.expires_at).map((t, i): Firing => ({ firing_id: `fir_ov${i}`, slot: t, trigger: 'cron', state: 'skipped', outcome: 'skipped', skip: 'OVERLAP', cost_usd: 0, work: '—' })) : []),
    ],
  } : s
  return (
    <>
      <div className="atl-crumb"><Button kind="ghost" size="sm" onClick={() => go({ tl: 'approvals' })}>Approvals</Button><span className="atl-dim">/</span><span className="atl-mono">{a.approval_id}</span></div>
      <PageHead title={merge ? `Merge ${pr}` : a.title} info={<InfoTip about="this approval" tip="The firing that asked waits here. Approve merges at the head shown; a new commit supersedes the request. Unanswered, it expires and the firing is skipped." />}>
        {state === 'superseded' && <Button kind="primary">Open the new request</Button>}
        {state === 'expired' && <Button>Run the schedule now</Button>}
      </PageHead>
      <div className={`atl-verdict is-${state}`} role={state === 'pending' ? 'status' : undefined}>
        {state === 'pending' && <><Glyph o="waits" /><b>{viewer ? `Waiting on ${OWNER}` : 'Waiting on you'}</b><span>expires {when(a.expires_at)} · {rel(a.expires_at)}</span><ExpiryBar a={a} /></>}
        {state === 'approved' && <><Glyph o="succeeded" /><b>Approved by {OWNER}</b><span>{when(decidedAt)} · merged at {head.asked}</span></>}
        {state === 'denied' && <><Glyph o="failed" /><b>Denied by {OWNER}</b><span>{when(decidedAt)} · “Hold workflow changes until the release”</span></>}
        {state === 'expired' && <><Glyph o="skipped" /><b>Expired</b><span>{when(a.expires_at)} · nobody decided in 72 h · the firing was skipped</span></>}
        {state === 'superseded' && <><Glyph o="skipped" /><b>Superseded</b><span>head moved {head.asked} → {head.now} · apr_r9w2e6 asks again</span></>}
      </div>
      <div className="atl-apgrid">
        <div className="atl-apmain">
          {merge && (
            <section className="atl-card">
              <div className="atl-card-h"><h2>Pull request {pr}</h2><span className="atl-dim">{a.summary}</span></div>
              <Facts rows={[
                ['Repository', 'example-org/example-api'],
                ['Branch', <code key="b">fix/upload-retry → main</code>],
                ['Head', state === 'superseded' ? <span key="h"><code>{head.asked}</code> <span className="atl-dim">now</span> <code>{head.now}</code></span> : <code key="h">{head.asked}</code>],
                ['Green at head', state === 'superseded' ? <span key="g" className="atl-dim">not at the new head</span> : <span key="g" className="atl-inl"><Glyph o="succeeded" />{head.asked} · 10 of 10 checks</span>],
                ['Changes', '4 files · +38 −6'],
              ]} />
            </section>
          )}
          {merge && (
            <section className="atl-card">
              <div className="atl-card-h"><h2>Why it is held</h2></div>
              <ul className="atl-held">{HELD_FILES.map((f) => <li key={f}><Glyph o="waits" /><code>{f}</code><span className="atl-dim">{f.startsWith('.github') ? 'workflow file' : 'shared script'}</span></li>)}</ul>
            </section>
          )}
          {merge && (
            <section className="atl-card">
              <div className="atl-card-h"><h2>Checks · 10 of 10</h2><span className="atl-dim">at {head.asked}</span></div>
              <ul className="atl-checks">{CHECKS.map((c) => <li key={c}><Glyph o="succeeded" /><span>{c}</span></li>)}</ul>
            </section>
          )}
        </div>
        <aside className="atl-apside">
          {state === 'pending' && (
            <section className="atl-card">
              <div className="atl-card-h"><h2>Decide</h2></div>
              <label className="atl-fld"><span>Reason</span><textarea rows={2} placeholder="Optional" disabled={viewer} /></label>
              {viewer
                ? <div className="atl-acts"><Button kind="danger" disabled>Deny</Button><Button kind="primary" disabledReason={`Owner only · ${OWNER} decides`}>{merge ? 'Approve and merge' : 'Approve'}</Button></div>
                : <div className="atl-acts"><Button kind="danger">Deny</Button><Button kind="primary">{merge ? 'Approve and merge' : 'Approve'}</Button></div>}
            </section>
          )}
          <section className="atl-card">
            <div className="atl-card-h"><h2>On the axis</h2></div>
            {axis && <div className="atl-bigaxis is-side"><AxisHead w={w} now={now} compact /><Lane s={axis} w={w} now={now} approvals={state === 'pending' ? [{ ...a, state: 'pending' }] : []} open={() => undefined} /></div>}
            <Facts rows={[
              ['Asked by', s ? <button key="s" type="button" className="atl-linkbtn" onClick={() => go({ tl: 'schedule', id: s.schedule_id })}>{s.name}</button> : a.requested_by],
              ['Firing', <span key="f" className="atl-mono">{firing?.firing_id ?? '—'} · {firing ? when(firing.slot) : ''}</span>],
              ['Asked', when(a.requested_at)],
              ['Approvers', a.approvers === 'owner_only' ? `Owner only · ${OWNER}` : approversWord(a.approvers)],
              ['Expires', `${when(a.expires_at)}`],
            ]} />
          </section>
        </aside>
      </div>
    </>
  )
}

/* ---- empty ---- */

function EmptyView({ st, go }: { st: 'none' | 'not-served'; go: (v: View) => void }) {
  const w: Win = { from: NOW - 3.5 * D, to: NOW + 3.5 * D }
  if (st === 'not-served') {
    return (
      <>
        <PageHead title="Schedules" />
        <div className="atl-emp">
          <h2>Automate is not served here</h2>
          <span className="atl-dim atl-mono">GET /v1/schedules · 404 · 12:00 <InfoTip about="not served" tip="This deployment’s API has no schedules route. An administrator turns Automate on in the deployment’s settings." /></span>
        </div>
      </>
    )
  }
  return (
    <>
      <PageHead title="Schedules" info={<InfoTip about="Schedules" tip="Schedules fire recurring work. Each one becomes a lane on this axis." />}>
        <Button kind="primary" onClick={() => go({ tl: 'edit', id: 'new' })}>New schedule</Button>
      </PageHead>
      <section className="atl-board" aria-label="Schedules on a time axis">
        <div className="atl-row atl-headrow"><span className="atl-c-name">Schedule</span><AxisHead w={w} /><span className="atl-c-spend">Today</span></div>
        <div className="atl-row atl-ghost"><span className="atl-c-name"><span className="atl-n">No schedules</span></span><div className="atl-lane"><Grid w={w} /></div><span className="atl-c-spend" /></div>
      </section>
      <div className="atl-starters">
        {[['failure-review', 'Failure review'], ['dependency-cve-refresh', 'Dependency refresh'], ['cost-report', 'Weekly cost report'], ['issue-plan-only', 'Plan open issues']].map(([t, n]) => (
          <button type="button" key={t} className="atl-starter" onClick={() => go({ tl: 'edit', id: 'new' })}><b>{n}</b><span className="atl-dim">{t}</span></button>
        ))}
      </div>
    </>
  )
}
