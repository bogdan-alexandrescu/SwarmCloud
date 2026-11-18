/**
 * VOICE IT › TELEMETRY (docs/design/voice-it-own-speech.md §12, build plan row
 * U4; the owner's pick of 5 November 2026, "cards and a timeline", as Settings
 * is drawn): what the speech machine did, over swarm-api's telemetry routes
 * (routes/voice_telemetry.py).
 *
 *   Live      the running session as it streams (`/live`): per-utterance
 *             latency, first audio, and the machine's 5 s readings;
 *   Days      one bar per day of the range (`/days`), coloured by nothing but
 *             the one figure picked, with the busiest day against the day cap;
 *   Sessions  the timeline (`/sessions`), newest first; the one picked opens
 *             into four cards: Models (end of speech to final words, first
 *             partial, real-time factors, first audio), Machine (peaks, model
 *             load and warm-up), Start (the start's phases) and Cost (minutes,
 *             the ledger's machine cost against both caps, stock-outs).
 *   Trace     debug mode's card (voice/DebugTrace.tsx, the owner's pick of 10
 *             November 2026, "Trace on Telemetry"): the recent traced
 *             sessions and one opened, every planner turn in full.
 *
 * EVERY NUMBER IS SERVED. Percentiles, minutes, days and cost are computed by
 * swarm-api on read (speech_telemetry.py); the page adds nothing up but the
 * range's totals, and a figure the server could not read (`machine_usd` or
 * `stockouts` null with its reason) is a dash with the reason in its tooltip,
 * never a zero.
 *
 * A FAILED READ IS NEVER DRAWN AS AN EMPTY TIMELINE: one line with its status,
 * and Retry. The empty state is one line.
 *
 * Platform admins only (the tab in Section.tsx), so every read is the platform
 * view (`scope=platform`): one machine, every workspace's sessions.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import {
  loadVoiceTelemetryDays,
  loadVoiceTelemetrySession,
  loadVoiceTelemetrySessions,
  normVoiceTelemetrySample,
  voiceTelemetryLiveUrl,
  type VoicePercentiles,
  type VoiceTelemetryDay,
  type VoiceTelemetrySample,
  type VoiceTelemetrySession,
  type VoiceTelemetrySessionDetail,
  type VoiceTimingName,
} from '../api'
import type { ApiError } from '../fetch'
import { PageHead } from '../Shell'
import { currentZone } from '../timezone'
import { clockTime } from '../types'
import { DebugTrace } from './DebugTrace'
import { Bars, CapMeter, PhaseTrack, Spark, Tile, orderedPhases, phaseWord, quantile, usd } from './telemetryCharts'
import '../styles/voice-settings.css'
import '../styles/voice-telemetry.css'

/**
 * The owner's caps (§1 decision record; swarm-api's voice/ledger.py
 * SESSION_CAP_USD and DAY_CAP_USD): the telemetry routes do not serve them, so
 * the page draws them from here.
 */
export const SESSION_CAP_USD = 100
export const DAY_CAP_USD = 300

const RANGES = [7, 30, 90] as const
type Range = (typeof RANGES)[number]
/** The live band keeps this many samples: an hour of 5 s readings. */
const LIVE_KEEP = 720

type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'idle'

function Tick({ tone, children, say }: { tone: Tone; children: ReactNode; say?: string }) {
  return (
    <span className={`vset-tick t-${tone}`} title={say}>
      <i aria-hidden="true" />
      {children}
    </span>
  )
}

function Card({ title, id, aside, children }: { title: string; id: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="vset-card" aria-labelledby={`vtel-${id}`} data-card={id}>
      <header className="vset-card-head">
        <h2 id={`vtel-${id}`}>{title}</h2>
        {aside}
      </header>
      <div className="vset-card-body">{children}</div>
    </section>
  )
}

function Fail({ what, error, onRetry }: { what: string; error: ApiError; onRetry: () => void }) {
  return (
    <div className="vset-fail" role="alert">
      <Tick tone="bad" say={error.message}>
        {what} not read{error.httpStatus !== null ? ` · ${error.httpStatus}` : ''}
      </Tick>
      <button type="button" className="vset-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

// ---- figures ---------------------------------------------------------------

const ms = (v: number | null): string => (v === null ? '—' : `${Math.round(v)} ms`)
const ratio = (v: number | null): string => (v === null ? '—' : v.toFixed(2))
const gib = (mib: number | null): string => (mib === null ? '—' : `${(mib / 1024).toFixed(1)} GiB`)
const pct = (v: number | null): string => (v === null ? '—' : `${Math.round(v)}%`)
const mins = (v: number): string => `${v < 10 ? v.toFixed(1) : Math.round(v)} min`
const when = (iso: string | null): string => clockTime(iso)?.text ?? '—'
const REASON: Readonly<Record<string, string>> = {
  ledger_unreadable: 'The ledger could not be read',
  audit_unreadable: "The machine's audit could not be read",
}
const why = (reason: string | null): string | undefined => (reason === null ? undefined : (REASON[reason] ?? reason))

/** The viewer's day in `tz`, `days` back, as the routes take it (YYYY-MM-DD). */
export function dayIn(tz: string, back = 0, now: number = Date.now()): string {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(now))
  const d = new Date(`${today}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() - back)
  return d.toISOString().slice(0, 10)
}

/** The rows of the Models card: each per-utterance and per-synthesis figure, in the order §12.2 lists them. */
const TIMINGS: readonly { name: VoiceTimingName; label: string; show: (v: number | null) => string }[] = [
  { name: 'stt_final_ms', label: 'End of speech → words', show: ms },
  { name: 'stt_first_partial_ms', label: 'First partial', show: ms },
  { name: 'stt_rtf', label: 'Speech real-time factor', show: ratio },
  { name: 'tts_first_audio_ms', label: 'First audio', show: ms },
  { name: 'tts_rtf', label: 'Voice real-time factor', show: ratio },
]

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const dig = (data: Record<string, unknown>, ...path: string[]): number | null => {
  let at: unknown = data
  for (const k of path) at = at !== null && typeof at === 'object' ? (at as Record<string, unknown>)[k] : undefined
  return num(at)
}
const series = (samples: readonly VoiceTelemetrySample[], kind: string, ...path: string[]): number[] =>
  samples.filter((s) => s.kind === kind).map((s) => dig(s.data, ...path)).filter((v): v is number => v !== null)

// ---- live --------------------------------------------------------------------

interface Live {
  state: 'unread' | 'idle' | 'session'
  sessionId: string | null
  word: string | null
  samples: VoiceTelemetrySample[]
}

/**
 * The live stream, through one `EventSource`. The server ends the stream with
 * `idle` or `end`; EventSource opens it again by itself, so a session that
 * starts while the page is open appears without a reload.
 */
function useLive(): Live {
  const [live, setLive] = useState<Live>({ state: 'unread', sessionId: null, word: null, samples: [] })
  useEffect(() => {
    if (typeof EventSource === 'undefined') return
    const source = new EventSource(voiceTelemetryLiveUrl())
    const parse = (e: Event): Record<string, unknown> => {
      try {
        const v: unknown = JSON.parse(String((e as MessageEvent).data))
        return v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {}
      } catch {
        return {}
      }
    }
    source.addEventListener('idle', () => setLive({ state: 'idle', sessionId: null, word: null, samples: [] }))
    source.addEventListener('end', () => setLive({ state: 'idle', sessionId: null, word: null, samples: [] }))
    source.addEventListener('session', (e) => {
      const d = parse(e)
      const id = typeof d.session_id === 'string' ? d.session_id : null
      const word = typeof d.state === 'string' ? d.state.toLowerCase() : null
      setLive((was) => ({ state: 'session', sessionId: id, word, samples: was.sessionId === id ? was.samples : [] }))
    })
    source.addEventListener('sample', (e) => {
      const s = normVoiceTelemetrySample(parse(e))
      setLive((was) => (was.samples.some((x) => x.seq === s.seq) ? was : { ...was, state: 'session', samples: [...was.samples, s].slice(-LIVE_KEEP) }))
    })
    return () => source.close()
  }, [])
  return live
}

/** The load tiles over a run of `metrics` samples: GPU, GPU memory, CPU, memory, disk. */
function LoadTiles({ samples }: { samples: readonly VoiceTelemetrySample[] }) {
  const metrics = samples.filter((s) => s.kind === 'metrics')
  const last = metrics[metrics.length - 1]?.data ?? {}
  const gpu = series(samples, 'metrics', 'gpu', 'util_pct')
  const gpuMem = series(samples, 'metrics', 'gpu', 'mem_used_mib')
  const cpu = series(samples, 'metrics', 'cpu_pct')
  const mem = series(samples, 'metrics', 'memory', 'used_mib')
  if (metrics.length === 0) return null
  return (
    <div className="vtel-tiles">
      <Tile k="GPU" v={pct(dig(last, 'gpu', 'util_pct'))}>
        <Spark data={gpu} max={100} tone={2} label="GPU" unit="%" />
      </Tile>
      <Tile k="GPU memory" v={`${gib(dig(last, 'gpu', 'mem_used_mib'))} / ${gib(dig(last, 'gpu', 'mem_total_mib'))}`}>
        <Spark data={gpuMem} max={dig(last, 'gpu', 'mem_total_mib')} tone={2} label="GPU memory" unit="MiB" />
      </Tile>
      <Tile k="CPU" v={pct(dig(last, 'cpu_pct'))}>
        <Spark data={cpu} max={100} tone={5} label="CPU" unit="%" />
      </Tile>
      <Tile k="Memory" v={`${gib(dig(last, 'memory', 'used_mib'))} / ${gib(dig(last, 'memory', 'total_mib'))}`}>
        <Spark data={mem} max={dig(last, 'memory', 'total_mib')} tone={5} label="Memory" unit="MiB" />
      </Tile>
      <Tile k="Disk" v={`${dig(last, 'disk', 'used_gb') ?? '—'} / ${dig(last, 'disk', 'total_gb') ?? '—'} GB`} />
    </div>
  )
}

/** Per-utterance end of speech to words and per-synthesis first audio, each with its p90 rule. */
function LatencyBars({ samples }: { samples: readonly VoiceTelemetrySample[] }) {
  const finals = series(samples, 'stt', 'final_ms')
  const first = series(samples, 'tts', 'first_audio_ms')
  const rule = (v: readonly number[]) => {
    const p = quantile(v, 0.9)
    return p === null ? null : { value: p, say: `p90 ${Math.round(p)} ms` }
  }
  if (finals.length === 0 && first.length === 0) return null
  return (
    <div className="vtel-figs">
      {finals.length > 0 && (
        <figure className="vtel-fig">
          <figcaption>End of speech → words</figcaption>
          <Bars testId="vtel-utterances" data={finals} labels={finals.map((_, i) => `Utterance ${i + 1}`)} tone={1} label="End of speech to final words, per utterance" unit="ms" rule={rule(finals)} />
        </figure>
      )}
      {first.length > 0 && (
        <figure className="vtel-fig">
          <figcaption>First audio</figcaption>
          <Bars testId="vtel-syntheses" data={first} labels={first.map((_, i) => `Synthesis ${i + 1}`)} tone={3} label="Text-to-speech first audio, per synthesis" unit="ms" rule={rule(first)} />
        </figure>
      )}
    </div>
  )
}

function LiveCard({ live }: { live: Live }) {
  if (live.state === 'unread') return null
  if (live.state === 'idle') {
    return (
      <section className="vtel-live is-idle" aria-label="Live" data-live="idle">
        <Tick tone="idle">Stopped</Tick>
      </section>
    )
  }
  const utterances = live.samples.filter((s) => s.kind === 'stt').length
  return (
    <section className="vtel-live" aria-label="Live" data-live="session">
      <div className="vtel-live-head">
        <span className="vtel-pulse" aria-hidden="true" />
        <Tick tone="info">{live.word ?? 'running'}</Tick>
        <span className="vtel-sub">{utterances} utterances</span>
      </div>
      <LatencyBars samples={live.samples} />
      <LoadTiles samples={live.samples} />
    </section>
  )
}

// ---- days ----------------------------------------------------------------------

type DayMetric = 'minutes' | 'cost' | 'utterances' | 'p90' | 'stockouts'
const DAY_METRICS: readonly { key: DayMetric; label: string; unit: string; of: (d: VoiceTelemetryDay) => number | null }[] = [
  { key: 'minutes', label: 'Minutes', unit: 'min', of: (d) => d.minutes },
  { key: 'cost', label: 'Cost', unit: 'USD', of: (d) => d.machine_usd },
  { key: 'utterances', label: 'Utterances', unit: '', of: (d) => d.utterances },
  { key: 'p90', label: 'p90', unit: 'ms', of: (d) => d.stt_final_ms_p90 },
  { key: 'stockouts', label: 'Stock-outs', unit: '', of: (d) => d.stockouts },
]

function DaysCard({ days, range }: { days: readonly VoiceTelemetryDay[]; range: Range }) {
  const [metric, setMetric] = useState<DayMetric>('minutes')
  const m = DAY_METRICS.find((x) => x.key === metric) ?? DAY_METRICS[0]!
  const costs = days.map((d) => d.machine_usd)
  const costKnown = costs.every((c) => c !== null)
  const busiest = days.reduce<VoiceTelemetryDay | null>((a, d) => (d.machine_usd !== null && (a === null || d.machine_usd > (a.machine_usd ?? 0)) ? d : a), null)
  const stockouts = days.every((d) => d.stockouts !== null) ? days.reduce((a, d) => a + (d.stockouts ?? 0), 0) : null
  const minutes = days.reduce((a, d) => a + d.minutes, 0)
  return (
    <Card
      title="Days"
      id="days"
      aside={
        <div className="vset-seg" role="radiogroup" aria-label="Day figure">
          {DAY_METRICS.map((x) => (
            <button key={x.key} type="button" role="radio" aria-checked={x.key === metric} className={x.key === metric ? 'is-on' : ''} onClick={() => setMetric(x.key)}>
              {x.label}
            </button>
          ))}
        </div>
      }
    >
      <div className="vtel-tot" data-testid="vtel-days-total">
        <span>{range} days</span>
        <span>{mins(minutes)}</span>
        <span title={costKnown ? undefined : why(days.find((d) => d.machine_usd === null)?.machine_usd_reason ?? null)}>
          {costKnown ? usd(costs.reduce<number>((a, c) => a + (c ?? 0), 0)) : '—'}
        </span>
        <span>{days.reduce((a, d) => a + d.utterances, 0)} utterances</span>
        <span title={stockouts === null ? why('audit_unreadable') : undefined}>{stockouts ?? '—'} stock-outs</span>
      </div>
      <Bars testId="vtel-days-chart" data={days.map(m.of)} labels={days.map((d) => d.day)} tone={1} label={`${m.label} per day`} unit={m.unit} />
      <CapMeter testId="vtel-cap-day-busiest" label={busiest ? `Busiest · ${busiest.day}` : 'Busiest day'} spent={busiest?.machine_usd ?? null} cap={DAY_CAP_USD} />
    </Card>
  )
}

// ---- sessions ----------------------------------------------------------------

function SessionRow({ s, on, onPick }: { s: VoiceTelemetrySession; on: boolean; onPick: () => void }) {
  return (
    <li className={`vtel-srow${on ? ' is-on' : ''}`} data-session={s.session_id}>
      <button type="button" className="vtel-sline" aria-pressed={on} onClick={onPick}>
        <span className="vtel-when">{when(s.first_at)}</span>
        <span className="vtel-num">{mins(s.minutes)}</span>
        <span className="vtel-num">{s.utterances} utt.</span>
        <span className="vtel-num" title="p90, end of speech to words">
          {ms(s.percentiles.stt_final_ms.p90)}
        </span>
        <span className="vtel-num" title={why(s.machine_usd_reason)}>
          {s.machine_usd === null ? '—' : usd(s.machine_usd)}
        </span>
        {s.stockouts !== null && s.stockouts > 0 && <Tick tone="warn">{s.stockouts} stock-out</Tick>}
      </button>
    </li>
  )
}

function PercentileTable({ p }: { p: Record<VoiceTimingName, VoicePercentiles> }) {
  return (
    <table className="vtel-pct">
      <thead>
        <tr>
          <th />
          <th>p50</th>
          <th>p90</th>
          <th>max</th>
        </tr>
      </thead>
      <tbody>
        {TIMINGS.map((t) => (
          <tr key={t.name} data-metric={t.name}>
            <th>{t.label}</th>
            <td>{t.show(p[t.name].p50)}</td>
            <td>{t.show(p[t.name].p90)}</td>
            <td>{t.show(p[t.name].max)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SessionCards({ s, day, detail, detailError, onRetry }: { s: VoiceTelemetrySession; day: VoiceTelemetryDay | null; detail: VoiceTelemetrySessionDetail | null; detailError: ApiError | null; onRetry: () => void }) {
  const samples = detail?.samples ?? []
  const phases = orderedPhases(s.start_phases)
  const startTotal = phases.reduce((a, [, v]) => a + v, 0)
  const load = s.start_phases['start.load'] ?? null
  const warm = s.start_phases['start.warmup'] ?? null
  const peaks = s.peaks
  return (
    <>
      {detailError !== null && <Fail what="Samples" error={detailError} onRetry={onRetry} />}
      <div className="vset-grid">
        <Card title="Models" id="models">
          <PercentileTable p={s.percentiles} />
          <LatencyBars samples={samples} />
        </Card>
        <Card title="Machine" id="machine">
          <dl className="vtel-dl">
            <dt>GPU</dt>
            <dd data-peak="gpu_util_pct">{pct(peaks.gpu_util_pct)}</dd>
            <dt>GPU memory</dt>
            <dd data-peak="gpu_mem_used_mib">{gib(peaks.gpu_mem_used_mib)}</dd>
            <dt>CPU</dt>
            <dd data-peak="cpu_pct">{pct(peaks.cpu_pct)}</dd>
            <dt>Memory</dt>
            <dd data-peak="memory_used_mib">{gib(peaks.memory_used_mib)}</dd>
            <dt>Disk</dt>
            <dd data-peak="disk_used_gb">{peaks.disk_used_gb === null ? '—' : `${peaks.disk_used_gb} GB`}</dd>
            <dt>vLLM queue</dt>
            <dd data-peak="vllm_queue">{peaks.vllm_queue ?? '—'}</dd>
            <dt>KV cache</dt>
            <dd data-peak="kv_cache_pct">{pct(peaks.kv_cache_pct)}</dd>
            <dt>{phaseWord('start.load')}</dt>
            <dd data-warm="load">{load === null ? '—' : `${load.toFixed(1)} s`}</dd>
            <dt>{phaseWord('start.warmup')}</dt>
            <dd data-warm="warmup">{warm === null ? '—' : `${warm.toFixed(1)} s`}</dd>
          </dl>
          <LoadTiles samples={samples} />
        </Card>
        <Card title="Start" id="start" aside={phases.length > 0 ? <span className="vtel-sub">{startTotal.toFixed(1)} s</span> : undefined}>
          {phases.length > 0 ? <PhaseTrack phases={s.start_phases} /> : <span className="vtel-sub">—</span>}
        </Card>
        <Card title="Cost" id="cost">
          <dl className="vtel-dl">
            <dt>Minutes</dt>
            <dd>{mins(s.minutes)}</dd>
            <dt>Machine</dt>
            <dd data-testid="vtel-session-usd" title={why(s.machine_usd_reason)}>
              {s.machine_usd === null ? '—' : usd(s.machine_usd)}
            </dd>
            <dt>Stock-outs</dt>
            <dd data-testid="vtel-session-stockouts" title={why(s.stockouts_reason)}>
              {s.stockouts ?? '—'}
            </dd>
            <dt>Errors</dt>
            <dd>{s.stt_errors + s.tts_errors}</dd>
          </dl>
          <CapMeter testId="vtel-cap-session" label="Session" spent={s.machine_usd} cap={SESSION_CAP_USD} why={why(s.machine_usd_reason)} />
          <CapMeter testId="vtel-cap-day" label={s.day ?? 'Day'} spent={day?.machine_usd ?? null} cap={DAY_CAP_USD} why={why(day?.machine_usd_reason ?? null)} />
        </Card>
      </div>
    </>
  )
}

// ---- the page ------------------------------------------------------------------

interface Read {
  days: VoiceTelemetryDay[]
  sessions: VoiceTelemetrySession[]
}

export function VoiceTelemetry() {
  const tz = useMemo(() => currentZone(), [])
  const [range, setRange] = useState<Range>(30)
  const [data, setData] = useState<Read | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [detail, setDetail] = useState<VoiceTelemetrySessionDetail | null>(null)
  const [detailError, setDetailError] = useState<ApiError | null>(null)
  const live = useLive()

  const load = useCallback(async () => {
    const span = { from: dayIn(tz, range - 1), to: dayIn(tz), tz }
    const [d, s] = await Promise.all([loadVoiceTelemetryDays(span), loadVoiceTelemetrySessions(span)])
    if (d.status === 'error' || s.status === 'error') {
      setError(d.status === 'error' ? d.error : s.status === 'error' ? s.error : null)
      setData(null)
      return
    }
    if (d.status !== 'ok' && d.status !== 'stale') return
    if (s.status !== 'ok' && s.status !== 'stale') return
    setError(null)
    setData({ days: d.data, sessions: s.data })
  }, [tz, range])

  useEffect(() => {
    void load()
  }, [load])

  const session = data?.sessions.find((x) => x.session_id === picked) ?? data?.sessions[0] ?? null
  const sessionId = session?.session_id ?? null

  const loadDetail = useCallback(async () => {
    if (sessionId === null) return
    const r = await loadVoiceTelemetrySession(sessionId, tz)
    if (r.status === 'error') {
      setDetail(null)
      setDetailError(r.error)
    } else if (r.status === 'ok' || r.status === 'stale') {
      setDetailError(null)
      setDetail(r.data)
    }
  }, [sessionId, tz])

  useEffect(() => {
    setDetail(null)
    setDetailError(null)
    void loadDetail()
  }, [loadDetail])

  const head = (
    <PageHead title="Telemetry">
      <div className="vset-seg" role="radiogroup" aria-label="Range">
        {RANGES.map((r) => (
          <button key={r} type="button" role="radio" aria-checked={r === range} className={r === range ? 'is-on' : ''} onClick={() => setRange(r)}>
            {r} days
          </button>
        ))}
      </div>
    </PageHead>
  )

  if (error !== null) {
    return (
      <div className="vset vtel">
        {head}
        <Fail what="Telemetry" error={error} onRetry={() => void load()} />
      </div>
    )
  }
  if (data === null) return <div className="vset vtel">{head}</div>

  const empty = data.sessions.length === 0 && data.days.every((d) => d.sessions === 0 && !d.stockouts && !d.machine_usd)
  if (empty && live.state !== 'session') {
    return (
      <div className="vset vtel">
        {head}
        <p className="vtel-empty">No sessions yet</p>
        <DebugTrace sessionId={null} quiet />
      </div>
    )
  }
  const day = session === null ? null : (data.days.find((d) => d.day === session.day) ?? null)
  return (
    <div className="vset vtel">
      {head}
      <LiveCard live={live} />
      <DaysCard days={data.days} range={range} />
      {data.sessions.length > 0 && (
        <section className="vtel-sessions" aria-label="Sessions">
          <ol className="vtel-slist">
            {data.sessions.map((s) => (
              <SessionRow key={s.session_id} s={s} on={s.session_id === sessionId} onPick={() => setPicked(s.session_id)} />
            ))}
          </ol>
        </section>
      )}
      {session !== null && <SessionCards s={session} day={day} detail={detail} detailError={detailError} onRetry={() => void loadDetail()} />}
      <DebugTrace sessionId={sessionId} />
    </div>
  )
}
