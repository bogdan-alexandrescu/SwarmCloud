// A `GET /v1/outcomes` payload in the contract's exact shape, for the dev
// fixtures (api.ts, when no API is reachable) and for the component tests.
//
// THE DAILY SPLIT IS THE DEV DEPLOYMENT'S, 12-25 Sep 2026, as the #185 design
// review read it: 272 succeeded, 28 failed, 416 cancelled, 730 submitted, the
// first task on 16 Sep -- so the headline reads 90.7 % (272 of 300) and 22 Sep
// carries 305 cancels beside its 8 failures. The mockup placed those counts by
// submission day; nothing here claims to be a completed_at read, and the
// cost, class and latency figures are placeholders chosen to sum to the
// totals. A test that needs another shape passes `over` or edits the result.

import type {
  CancelSplit,
  FailureClassKey,
  LedgerBucketSize,
  OutcomeBucket,
  Outcomes,
  Rate,
} from './outcomes'

/** Wilson 95 %, z = 1.959964, 4 dp -- the contract's rule, for the fixture only. The UI never computes it. */
export function wilsonFixture(k: number, n: number): Rate | null {
  if (n === 0) return null
  const z = 1.959964
  const p = k / n
  const d = 1 + (z * z) / n
  const c = (p + (z * z) / (2 * n)) / d
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d
  const r4 = (v: number) => Math.round(v * 10_000) / 10_000
  return { k, n, p: r4(p), lo: r4(Math.max(0, c - h)), hi: r4(Math.min(1, c + h)) }
}

const ZERO_CLASSES: Record<FailureClassKey, number> = {
  runner_error: 0,
  timeout: 0,
  lost_worker: 0,
  could_not_start: 0,
  inputs_unavailable: 0,
  outputs_missing: 0,
  dispatch_failed: 0,
  other: 0,
  no_reason: 0,
}

interface Day {
  day: number
  s: number
  f: number
  sub: number
  c: Omit<CancelSplit, 'total'>
  cls?: Partial<Record<FailureClassKey, number>>
  cost?: { sum: number | null; attempts: number; reporting: number }
}

const NONE = { requested: 0, after_failure: 0, after_cancel: 0, workflow_sweep: 0, other: 0 }

/** 12-25 Sep. Sums: succeeded 272, failed 28, submitted 730, cancelled 401 / 12 / 3 / 0. */
export const DEV_DAYS: readonly Day[] = [
  { day: 12, s: 0, f: 0, sub: 0, c: NONE },
  { day: 13, s: 0, f: 0, sub: 0, c: NONE },
  { day: 14, s: 0, f: 0, sub: 0, c: NONE },
  { day: 15, s: 0, f: 0, sub: 0, c: NONE },
  { day: 16, s: 1, f: 0, sub: 2, c: NONE, cost: { sum: 0.42, attempts: 1, reporting: 1 } },
  { day: 17, s: 1, f: 0, sub: 1, c: NONE, cost: { sum: 0.1, attempts: 1, reporting: 1 } },
  { day: 18, s: 18, f: 0, sub: 20, c: NONE, cost: { sum: 24.5, attempts: 22, reporting: 14 } },
  { day: 19, s: 15, f: 1, sub: 18, c: { ...NONE, requested: 1 }, cls: { other: 1 }, cost: { sum: 31.2, attempts: 20, reporting: 12 } },
  {
    day: 20, s: 22, f: 2, sub: 40, c: { ...NONE, requested: 12, after_failure: 1 },
    cls: { runner_error: 1, other: 1 }, cost: { sum: 44.8, attempts: 38, reporting: 20 },
  },
  { day: 21, s: 2, f: 0, sub: 3, c: NONE, cost: { sum: 3.3, attempts: 2, reporting: 2 } },
  {
    day: 22, s: 25, f: 8, sub: 338, c: { requested: 297, after_failure: 5, after_cancel: 0, workflow_sweep: 3, other: 0 },
    cls: { dispatch_failed: 8 }, cost: { sum: 61.4, attempts: 240, reporting: 30 },
  },
  { day: 23, s: 22, f: 6, sub: 32, c: { ...NONE, requested: 1, after_failure: 1 }, cls: { lost_worker: 6 }, cost: { sum: 52.1, attempts: 44, reporting: 31 } },
  { day: 24, s: 55, f: 2, sub: 84, c: { ...NONE, requested: 23, after_failure: 2 }, cls: { other: 2 }, cost: { sum: 70.2, attempts: 96, reporting: 40 } },
  {
    day: 25, s: 111, f: 9, sub: 192, c: { ...NONE, requested: 67, after_failure: 3 },
    cls: { lost_worker: 1, outputs_missing: 6, other: 2 }, cost: { sum: 95.82, attempts: 148, reporting: 59 },
  },
]

const iso = (day: number) => `2026-09-${String(day).padStart(2, '0')}T00:00:00+03:00`

function bucketOf(d: Day, generatedAt: number): OutcomeBucket {
  const cancelled: CancelSplit = {
    total: d.c.requested + d.c.after_failure + d.c.after_cancel + d.c.workflow_sweep + d.c.other,
    ...d.c,
  }
  const end = iso(d.day + 1)
  const endMs = Date.parse(end)
  return {
    start: iso(d.day),
    end,
    state: endMs + 900_000 <= generatedAt ? 'sealed' : 'open',
    in_progress: endMs > generatedAt,
    unread_reason: null,
    submitted: d.sub,
    ended: d.s + d.f + cancelled.total,
    succeeded: d.s,
    failed: d.f,
    dead_lettered: 0,
    cancelled,
    rate: wilsonFixture(d.s, d.s + d.f),
    failure_classes: { ...ZERO_CLASSES, ...(d.cls ?? {}) },
    cost: d.cost === undefined ? { sum_usd: null, attempts: 0, reporting: 0 } : { sum_usd: d.cost.sum, attempts: d.cost.attempts, reporting: d.cost.reporting },
  }
}

/** The payload the Timeline reads, as the dev deployment would serve it for `tz=Europe/Bucharest&span=14d`. */
export function ledgerFixture(): Outcomes {
  const generated_at = '2026-09-25T11:10:02Z'
  const gen = Date.parse(generated_at)
  const buckets = DEV_DAYS.map((d) => bucketOf(d, gen))
  const sum = (f: (b: OutcomeBucket) => number) => buckets.reduce((n, b) => n + f(b), 0)
  const classes = { ...ZERO_CLASSES }
  for (const b of buckets) for (const k of Object.keys(classes) as FailureClassKey[]) classes[k] += b.failure_classes?.[k] ?? 0
  const succeeded = sum((b) => b.succeeded ?? 0)
  const failed = sum((b) => b.failed ?? 0)
  const cancel = (k: keyof CancelSplit) => sum((b) => b.cancelled?.[k] ?? 0)
  const bucket: LedgerBucketSize = 'day'
  return {
    scope: { kind: 'tenant', tenant_id: 'eng' },
    tz: 'Europe/Bucharest',
    requested: { span: '14d', since: null, until: null, bucket: 'auto' },
    since: '2026-09-12T00:00:00+03:00',
    until: '2026-09-25T14:10:02+03:00',
    bucket,
    bucket_chosen_by: 'server',
    basis: { outcomes: 'completed_at', submitted: 'created_at' },
    filters: { profile: [], submitted_by: [], kind: 'all', tenant: [], exclude_tenant: [], group: 'runner_profile' },
    vocab: {
      failure_classes: [
        { key: 'runner_error', label: 'runner error' },
        { key: 'timeout', label: 'timeout' },
        { key: 'lost_worker', label: 'lost worker' },
        { key: 'could_not_start', label: 'could not start' },
        { key: 'inputs_unavailable', label: 'inputs unavailable' },
        { key: 'outputs_missing', label: 'outputs missing' },
        { key: 'dispatch_failed', label: 'dispatch failed' },
        { key: 'other', label: 'other' },
        { key: 'no_reason', label: 'no reason recorded' },
      ],
      cancel_causes: [
        { key: 'requested', label: 'requested' },
        { key: 'after_failure', label: 'after a failure' },
        { key: 'after_cancel', label: 'after a cancel' },
        { key: 'workflow_sweep', label: 'workflow sweep' },
        { key: 'other', label: 'other' },
      ],
      classifier_version: 2,
    },
    buckets,
    totals: {
      complete: true,
      buckets: buckets.length,
      buckets_read: buckets.length,
      submitted: sum((b) => b.submitted ?? 0),
      ended: sum((b) => b.ended ?? 0),
      succeeded,
      failed,
      dead_lettered: 0,
      cancelled: {
        total: cancel('total'),
        requested: cancel('requested'),
        after_failure: cancel('after_failure'),
        after_cancel: cancel('after_cancel'),
        workflow_sweep: cancel('workflow_sweep'),
        other: cancel('other'),
      },
      rate: wilsonFixture(succeeded, succeeded + failed),
      failure_classes: classes,
      cost: {
        sum_usd: 383.84,
        attempts: 612,
        reporting: 209,
        by_outcome: {
          succeeded: { sum_usd: 301.1, attempts: 330, reporting: 150 },
          failed: { sum_usd: 22.6, attempts: 60, reporting: 25 },
          dead_lettered: { sum_usd: null, attempts: 0, reporting: 0 },
          cancelled: { sum_usd: 60.14, attempts: 222, reporting: 34 },
        },
        per_succeeded_task: { n: 112, of: 272, p50_usd: 1.62, p95_usd: 5.8, max_usd: 12.4, values_usd: null },
        retries: { sum_usd: 31.2, attempts: 40, reporting: 22 },
        declared: { profiles: ['mock'], sum_usd: 0.24, attempts: 380, reporting: 24 },
      },
    },
    retries: {
      tries: [
        { attempts: '0', tasks: 398, succeeded: 0, failed: 0, dead_lettered: 0, cancelled: 398 },
        { attempts: '1', tasks: 300, succeeded: 262, failed: 20, dead_lettered: 0, cancelled: 18 },
        { attempts: '2', tasks: 12, succeeded: 6, failed: 6, dead_lettered: 0, cancelled: 0 },
        { attempts: '3+', tasks: 6, succeeded: 4, failed: 2, dead_lettered: 0, cancelled: 0 },
      ],
      needed_retry: { k: 18, of: 318 },
      rescued: 10,
      failed_after_retry: 8,
      not_final: {
        attempts: 41,
        by_exit: [
          { exit_code: 75, label: 'parked', n: 14 },
          { exit_code: null, label: 'no exit recorded', n: 9 },
          // The route's own labels (swarm_api.outcomes EXIT_LABELS), so a
          // fixture read shows what a live one does.
          { exit_code: 143, label: 'interrupted', n: 7 },
          { exit_code: 69, label: 'dependency unavailable', n: 6 },
          { exit_code: 70, label: 'generation fenced', n: 5 },
        ],
      },
      admissions_without_attempt_doc: 0,
    },
    latency: {
      percentile_method: 'nearest_rank',
      by_profile: [
        {
          runner_profile: 'mock',
          timeout_s: 600,
          succeeded: {
            n: 190,
            wait: { n: 190, p50_s: 2, p95_s: 40, max_s: 120, values_s: null },
            run: { n: 190, p50_s: 8, p95_s: 63, max_s: 300, values_s: null },
            total: { n: 190, p50_s: 12, p95_s: 110, max_s: 400, values_s: null },
          },
          failed: {
            n: 2,
            wait: { n: 2, p50_s: 3, p95_s: 4, max_s: 4, values_s: [3, 4] },
            run: { n: 2, p50_s: 31, p95_s: 40, max_s: 40, values_s: [31, 40] },
            total: { n: 2, p50_s: 34, p95_s: 44, max_s: 44, values_s: [34, 44] },
          },
        },
        {
          runner_profile: 'claude-code',
          timeout_s: 7200,
          succeeded: {
            n: 61,
            wait: { n: 61, p50_s: 48, p95_s: 1460, max_s: 2100, values_s: null },
            run: { n: 61, p50_s: 660, p95_s: 3120, max_s: 6900, values_s: null },
            total: { n: 61, p50_s: 720, p95_s: 3900, max_s: 7100, values_s: null },
          },
          failed: {
            n: 22,
            wait: { n: 22, p50_s: 50, p95_s: 900, max_s: 1300, values_s: null },
            run: { n: 22, p50_s: 1800, p95_s: 7200, max_s: 7210, values_s: null },
            total: { n: 22, p50_s: 1900, p95_s: 7300, max_s: 7400, values_s: null },
          },
        },
        {
          runner_profile: 'codex',
          timeout_s: null,
          succeeded: { n: 0, wait: null, run: null, total: null },
          failed: { n: 0, wait: null, run: null, total: null },
        },
      ],
    },
    groups: {
      by: 'runner_profile',
      rows_total: 4,
      rows: [
        {
          key: 'claude-code', submitted: 180, ended: 172, succeeded: 61, failed: 22, dead_lettered: 0,
          cancelled: { total: 89, requested: 80, after_failure: 9, after_cancel: 0, workflow_sweep: 0, other: 0 },
          rate: wilsonFixture(61, 83), cost: { sum_usd: 371.2, attempts: 190, reporting: 181 }, declared_cost: false,
          series: buckets.map((b) => (b.rate === null ? { k: 0, n: 0 } : { k: Math.min(b.rate.k, 6), n: Math.min(b.rate.n, 8) })),
        },
        {
          key: 'mock', submitted: 467, ended: 460, succeeded: 190, failed: 2, dead_lettered: 0,
          cancelled: { total: 268, requested: 265, after_failure: 3, after_cancel: 0, workflow_sweep: 0, other: 0 },
          rate: wilsonFixture(190, 192), cost: { sum_usd: 0.24, attempts: 380, reporting: 24 }, declared_cost: true,
          series: buckets.map((b) => (b.rate === null ? { k: 0, n: 0 } : { k: b.rate.n, n: b.rate.n })),
        },
        {
          key: 'codex', submitted: 4, ended: 4, succeeded: 0, failed: 0, dead_lettered: 0,
          cancelled: { total: 4, requested: 4, after_failure: 0, after_cancel: 0, workflow_sweep: 0, other: 0 },
          rate: null, cost: { sum_usd: 12.4, attempts: 4, reporting: 4 }, declared_cost: false, series: null,
        },
        {
          key: 'generic', submitted: 2, ended: 2, succeeded: 2, failed: 0, dead_lettered: 0,
          cancelled: { total: 0, requested: 0, after_failure: 0, after_cancel: 0, workflow_sweep: 0, other: 0 },
          rate: wilsonFixture(2, 2), cost: { sum_usd: null, attempts: 2, reporting: 0 }, declared_cost: false,
          // A null entry is an UNREAD bucket, and every bucket here was read:
          // measured {0, 0} until the day both of generic's tasks ended.
          series: buckets.map((_, i) => (i === buckets.length - 1 ? { k: 2, n: 2 } : { k: 0, n: 0 })),
        },
      ],
    },
    workflows_failed: {
      applicable: true,
      with_ended_steps: 38,
      with_failed_steps: 3,
      rows_total: 3,
      rows: [
        {
          workflow_id: 'wf_bcdc9180e4fb4a209f31', tenant_id: 'eng', submitted_by: 'engineer-a@saga.xyz',
          first_failed: { step_id: 'synthesis', task_id: 'task_0123456789abcdef0001', failure_class: 'lost_worker', ended_at: '2026-09-25T08:10:00Z' },
          cascade_cancelled: 2, last_ended_at: '2026-09-25T08:12:00Z', state: 'FAILED',
          steps: { total: 4, succeeded: 1, failed: 1, dead_lettered: 0, cancelled: 2, open: 0, unreadable: 0 },
        },
        {
          workflow_id: 'wf_71aa000000000000002c1', tenant_id: 'eng', submitted_by: 'engineer-b@saga.xyz',
          first_failed: { step_id: 'review', task_id: 'task_0123456789abcdef0002', failure_class: 'dispatch_failed', ended_at: '2026-09-22T10:00:00Z' },
          cascade_cancelled: 0, last_ended_at: '2026-09-22T10:05:00Z', state: 'FAILED',
          steps: { total: 4, succeeded: 3, failed: 1, dead_lettered: 0, cancelled: 0, open: 0, unreadable: 0 },
        },
        {
          workflow_id: 'wf_0c3e0000000000000077b0', tenant_id: 'eng', submitted_by: 'verify-sa@saga.xyz',
          first_failed: { step_id: 'build', task_id: 'task_0123456789abcdef0003', failure_class: 'lost_worker', ended_at: '2026-09-23T09:00:00Z' },
          cascade_cancelled: 1, last_ended_at: '2026-09-23T09:01:00Z', state: 'UNKNOWN', steps: null,
        },
      ],
      failing_steps: [
        { step_id: 'synthesis', n: 4 },
        { step_id: 'review', n: 2 },
        { step_id: 'build', n: 1 },
      ],
    },
    coverage: {
      // 12-25 Sep in Bucharest touches the UTC days 11-25 Sep. At 11:10Z on
      // the 25th every one but the 25th is past its seal grace.
      days: { total: 15, sealed: 14, live: 1, unread: 0 },
      unread: [],
      derived_now: 0,
      built_through: '2026-09-25T11:08:02Z',
      reopened: 0,
      terminal_without_completed_at: 0,
      wait_excluded: 0,
      seal_grace_s: 900,
    },
    previous: {
      since: '2026-08-29T00:00:00+03:00',
      until: '2026-09-12T00:00:00+03:00',
      complete: true,
      succeeded: 0,
      failed: 0,
      dead_lettered: 0,
      cancelled_total: 0,
      rate: null,
    },
    reads: 34,
    cached: false,
    generated_at,
  }
}
