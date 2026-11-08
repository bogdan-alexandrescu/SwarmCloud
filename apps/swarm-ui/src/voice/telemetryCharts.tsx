/**
 * VOICE IT › TELEMETRY'S CHARTS (docs/design/voice-it-own-speech.md §12): inline
 * SVG, no charting library. Each chart draws one series in one `--series-*`
 * token (stroke and fill only, never text), so its card's title names it and no
 * legend is needed; every mark carries its value in a `<title>`, which is the
 * hover. Axis and rule labels are HTML over the SVG, so they keep the console's
 * 12px floor at any width, and every SVG scales to its box
 * (`preserveAspectRatio="none"`), so nothing is wider than a phone.
 */
import type { ReactNode } from 'react'

export type Tone = 1 | 2 | 3 | 4 | 5

/** The value at quantile `q` of `values`, nearest rank; null for none. */
export function quantile(values: readonly number[], q: number): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))] ?? null
}

/** A 5 s reading over time: one 2px line, the area under it, the latest value dotted. */
export function Spark({ data, max, tone, label, unit }: { data: readonly number[]; max?: number | null; tone: Tone; label: string; unit: string }) {
  if (data.length === 0) return null
  const w = 120
  const h = 32
  const peak = Math.max(...data)
  const hi = max ?? (peak > 0 ? peak * 1.1 : 1)
  const x = (i: number) => (i / Math.max(1, data.length - 1)) * (w - 4) + 2
  const y = (v: number) => h - 2 - (Math.min(Math.max(v, 0), hi) / hi) * (h - 5)
  const d = data.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const last = data[data.length - 1] ?? 0
  const say = `${label}: latest ${last} ${unit}, peak ${peak} ${unit}`
  return (
    <svg className={`vtel-spark s-${tone}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={say}>
      <title>{say}</title>
      <path className="a" d={`${d}L${x(data.length - 1).toFixed(1)},${h}L${x(0).toFixed(1)},${h}Z`} />
      <path className="l" d={d} />
      <circle className="p" cx={x(data.length - 1)} cy={y(last)} r="2.5" />
    </svg>
  )
}

/**
 * One bar per event from a zero baseline (an utterance's latency, a day's
 * minutes), with an optional rule: the p90, or a cap. `labels[i]` names bar i
 * in its tooltip; a null value is a gap, never a zero.
 */
export function Bars({
  data,
  labels,
  tone,
  label,
  unit,
  rule,
  testId,
}: {
  data: readonly (number | null)[]
  labels?: readonly string[]
  tone: Tone
  label: string
  unit: string
  rule?: { value: number; say: string } | null
  testId?: string
}) {
  const w = 300
  const h = 80
  const known = data.filter((v): v is number => v !== null)
  const hi = Math.max(rule?.value ?? 0, ...known, 1) * 1.08
  const bw = w / Math.max(1, data.length)
  const y = (v: number) => h - (Math.max(0, v) / hi) * (h - 4)
  const top = (v: number) => `${(y(v) / h) * 100}%`
  const show = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(2))
  return (
    <div className={`vtel-bars s-${tone}`} data-testid={testId}>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={`${label}${rule ? `; ${rule.say}` : ''}`}>
        <line className="g" x1="0" x2={w} y1={h - 0.5} y2={h - 0.5} />
        {data.map((v, i) =>
          v === null ? null : (
            <rect key={i} className="b" x={i * bw + Math.min(1, bw * 0.15)} width={Math.max(1, bw - Math.min(2, bw * 0.3))} y={y(v)} height={Math.max(v > 0 ? 1 : 0, h - y(v))} rx="1.5">
              <title>{`${labels?.[i] ?? `${i + 1}`}: ${show(v)} ${unit}`}</title>
            </rect>
          ),
        )}
        {rule && <line className="r" x1="0" x2={w} y1={y(rule.value)} y2={y(rule.value)} />}
      </svg>
      {rule && (
        <span className="vtel-rule" style={{ top: top(rule.value) }}>
          {rule.say}
        </span>
      )}
    </div>
  )
}

/** The order a start's phases happen in (§4.2 step 5); any other the server adds follows. */
export const PHASE_ORDER = ['start.boot', 'start.server', 'start.update', 'start.fetch', 'start.load', 'start.warmup'] as const
const PHASE_WORD: Readonly<Record<string, string>> = {
  'start.boot': 'Boot',
  'start.server': 'Server',
  'start.update': 'Update',
  'start.fetch': 'Fetch',
  'start.load': 'Load',
  'start.warmup': 'Warm-up',
}

export function phaseWord(key: string): string {
  return PHASE_WORD[key] ?? key.replace(/^start\./, '')
}

/** The phases as served, in the order a start runs them. */
export function orderedPhases(phases: Readonly<Record<string, number>>): [string, number][] {
  const known = PHASE_ORDER.filter((k) => phases[k] !== undefined).map((k): [string, number] => [k, phases[k]!])
  const rest = Object.keys(phases)
    .filter((k) => !(PHASE_ORDER as readonly string[]).includes(k))
    .sort()
    .map((k): [string, number] => [k, phases[k]!])
  return [...known, ...rest]
}

const secs = (s: number): string => `${s < 10 ? s.toFixed(1) : Math.round(s)} s`

/** A start's breakdown: one track, a segment per phase sized by its seconds, and the list. */
export function PhaseTrack({ phases }: { phases: Readonly<Record<string, number>> }) {
  const rows = orderedPhases(phases)
  if (rows.length === 0) return null
  return (
    <div className="vtel-phases">
      <div className="vtel-ptrack" role="img" aria-label={rows.map(([k, s]) => `${phaseWord(k)} ${secs(s)}`).join(', ')}>
        {rows.map(([k, s], i) => (
          <span key={k} className={`vtel-pseg p-${(i % 5) + 1}`} style={{ flexGrow: Math.max(s, 0.01) }} title={`${phaseWord(k)}: ${secs(s)}`} />
        ))}
      </div>
      <ol className="vtel-plist">
        {rows.map(([k, s], i) => (
          <li key={k} data-phase={k}>
            <i className={`p-${(i % 5) + 1}`} aria-hidden="true" />
            <span>{phaseWord(k)}</span>
            <span className="vtel-num">{secs(s)}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

export const usd = (n: number): string => `$${n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString('en-US')}`

/** Spend against a cap: a meter filled to the share, the figure and the cap beside it. */
export function CapMeter({ label, spent, cap, why, testId }: { label: string; spent: number | null; cap: number; why?: string | null; testId?: string }) {
  const share = spent === null ? 0 : Math.min(1, Math.max(0, spent / cap))
  const level = share >= 1 ? 'is-bad' : share >= 0.8 ? 'is-warn' : ''
  return (
    <div className={`vtel-cap ${level}`} data-testid={testId}>
      <span className="vtel-cap-k">{label}</span>
      <span className="vtel-cap-bar" aria-hidden="true">
        <i style={{ width: `${(share * 100).toFixed(1)}%` }} />
      </span>
      <span className="vtel-cap-v" title={spent === null ? (why ?? undefined) : undefined}>
        {spent === null ? '—' : usd(spent)} / {usd(cap)}
      </span>
    </div>
  )
}

/** A reading's figure over its own small chart, for the load tiles. */
export function Tile({ k, v, children }: { k: string; v: ReactNode; children?: ReactNode }) {
  return (
    <div className="vtel-tile" data-tile={k}>
      <div className="vtel-tile-k">{k}</div>
      <div className="vtel-tile-v">{v}</div>
      {children}
    </div>
  )
}
