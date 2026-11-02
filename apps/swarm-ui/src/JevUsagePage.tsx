// JEV-USAGE-PAGE (part of #2688; the owner chose on 2 November 2026 to build
// a Jev usage and cost page): CAPACITY › JEV USAGE.
//
// No one could see how many calls, tokens or dollars Jev uses. This page draws
// `GET /v1/jev-usage` for the selected workspace: the 30-day total, the
// month's use against its cap, and a daily table by kind -- the worker's
// judgments by question, the quality gate, the agent's decision tool, the Jev
// browser -- with each call's outcome and every skip's reason.
//
// A DOLLAR IS A DASH, NEVER $0, UNTIL A RATE IS SET. Jev's price is the
// platform setting `jev_token_rates`, with no default; until it is set the
// page says "rate not set". Output tokens are a dash where no record carried
// them. Counts only: a record's task, payload or key never reaches this page.
//
// BREAK IT: print $0.00 without a rate, drop a skip's reason, or sum the
// table's rows from a different field than the total -- a case in
// jevusage.page.test.tsx turns red.

import { Card, Dash, Table, UsageBar, type Column } from './components'
import type { TopicId } from './help'
import { capacityPoll } from './capacityPoll'
import { Screen } from './Shell'
import {
  JEV_GROUP_LABEL,
  loadJevUsage,
  type JevKindRow,
  type JevOutcomes,
  type JevTally,
  type JevUsage,
} from './jevUsage'

const TOPICS: readonly TopicId[] = ['jev-usage', 'absent-vs-zero']
/** Jev's records land when an attempt settles: a minute is fresh enough. */
const JEV_USAGE_POLL_MS = 60_000
export const RATE_NOT_SET = 'rate not set'
const NO_OUTPUT = 'output tokens not recorded'

function n(v: number): string {
  return v.toLocaleString('en-US')
}

/** Dollars, or a dash saying why there are none: never $0 for "not priced". */
export function Dollars({ usd, rateSet }: { usd: number | null; rateSet: boolean }) {
  if (usd === null) return <Dash why={rateSet ? 'needs the output rate, which is not set' : RATE_NOT_SET} />
  return <>{`$${usd.toFixed(usd < 1 ? 4 : 2)}`}</>
}

function Tokens({ v }: { v: number | null }) {
  return v === null ? <Dash why={NO_OUTPUT} /> : <>{n(v)}</>
}

/** "2 ok · 1 failed · 3 skipped (oversize 2, budget_calls 1)". */
export function outcomeText(o: JevOutcomes): string {
  const parts = [`${n(o.ok)} ok`, `${n(o.failed)} failed`, `${n(o.skipped)} skipped`]
  const reasons = o.skippedByReason.map((r) => `${r.reason} ${n(r.count)}`).join(', ')
  return reasons === '' ? parts.join(' · ') : `${parts.join(' · ')} (${reasons})`
}

export function kindLabel(k: Pick<JevKindRow, 'kind' | 'group'>): string {
  const group = JEV_GROUP_LABEL[k.group]
  return k.group === 'worker_judgment' ? `${group} ${k.kind}` : group
}

interface DayRow extends JevKindRow {
  key: string
  day: string
}

function columns(rateSet: boolean): readonly Column<DayRow>[] {
  return [
    { key: 'day', head: 'Day (UTC)', cell: (r) => r.day },
    { key: 'kind', head: 'Kind', cell: (r) => kindLabel(r) },
    { key: 'calls', head: 'Calls', num: true, cell: (r) => n(r.calls) },
    { key: 'in', head: 'Tokens in', num: true, cell: (r) => n(r.inputTokens) },
    { key: 'out', head: 'Tokens out', num: true, cell: (r) => <Tokens v={r.outputTokens} /> },
    { key: 'outcomes', head: 'Outcomes', cell: (r) => outcomeText(r.outcomes) },
    { key: 'usd', head: 'Dollars', num: true, cell: (r) => <Dollars usd={r.usd} rateSet={rateSet} /> },
  ]
}

function TotalLine({ t, rateSet }: { t: JevTally; rateSet: boolean }) {
  return (
    <dl className="jev-usage-total">
      <dt>Calls</dt>
      <dd>{n(t.calls)}</dd>
      <dt>Tokens in</dt>
      <dd>{n(t.inputTokens)}</dd>
      <dt>Tokens out</dt>
      <dd>
        <Tokens v={t.outputTokens} />
      </dd>
      <dt>Dollars</dt>
      <dd>
        <Dollars usd={t.usd} rateSet={rateSet} />
      </dd>
      <dt>Outcomes</dt>
      <dd>{outcomeText(t.outcomes)}</dd>
    </dl>
  )
}

export function JevUsageBody({ usage }: { usage: JevUsage }) {
  const rateSet = usage.rate.set
  const rows: DayRow[] = usage.days.flatMap((d) =>
    d.kinds.map((k) => ({ ...k, key: `${d.day}/${k.kind}`, day: d.day })),
  )
  const m = usage.month
  return (
    <>
      <Card title={`Last ${usage.window.days} days`}>
        <TotalLine t={usage.total} rateSet={rateSet} />
        <p className="jev-usage-rate">
          {rateSet
            ? `Priced at $${usage.rate.input} per million tokens in${
                usage.rate.output === null ? ' (output rate not set)' : `, $${usage.rate.output} out`
              }.`
            : 'Jev’s price: rate not set. Dollars stay blank until a platform admin sets Jev’s per-token rate.'}
        </p>
      </Card>
      <Card title={`Monthly cap, ${m.month}`}>
        {m.capTokens === null ? (
          <p>
            {n(m.usedTokens)} tokens in this month. No monthly cap is set for this workspace.
          </p>
        ) : (
          <UsageBar label="Tokens in this month" used={m.usedTokens} ceiling={m.capTokens} unit="tokens" />
        )}
      </Card>
      <Card title="By day and kind">
        {rows.length === 0 ? (
          <p>No Jev calls were recorded for this workspace in the last {usage.window.days} days.</p>
        ) : (
          <Table columns={columns(rateSet)} rows={rows} rowKey={(r) => r.key} caption="Jev usage by day and kind" />
        )}
        {usage.undated !== null && (
          <p className="jev-usage-undated">
            Also {n(usage.undated.calls)} calls and {n(usage.undated.inputTokens)} tokens in whose records carry no day.
          </p>
        )}
      </Card>
    </>
  )
}

export function JevUsageScreen() {
  return (
    <Screen title="Jev usage" help={TOPICS} load={loadJevUsage} pollMs={capacityPoll(JEV_USAGE_POLL_MS)}>
      {(usage) => <JevUsageBody usage={usage} />}
    </Screen>
  )
}
