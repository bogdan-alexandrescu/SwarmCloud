// A task's CPU, memory and throttling over time, as `GET /v1/tasks/{id}/utilisation`
// serves it (LIVE-UTIL-API, #1696; the shape is `swarm_api.utilisation`), and
// the arithmetic Details says about it (LIVE-UTIL-UI; part of #1689).
//
// WHY THIS EXISTS. The owner, 2026-10-20: Details showed an attempt's CPU
// limit, the peak of its samples and its mean, and "those will always show
// maximum when in fact the pods do not run at max cpu the entire time". The
// worker now takes one point every 5 s from the container's cgroup -- cores
// used over the window, memory now, and the share of the window's CFS periods
// that were throttled -- and the route serves them per attempt.
//
// NULL IS "NOT MEASURED", NEVER ZERO, throughout, as the route says it: a
// point the worker could not read is served null, and so is a figure no
// heartbeat carried. Nothing here turns one into a 0.
//
// Pure: no React, no DOM, so the chart and its test share it.

/** One 5-second point. `at` is the server's dating of the window's end. */
export interface UtilisationPoint {
  at: string
  cpu_cores: number | null
  memory_bytes: number | null
  /** The share (0-1) of the window's CFS periods that were throttled. */
  throttled_share: number | null
}

/** One attempt's row, newest attempt first in `UtilisationRead.attempts`. */
export interface UtilisationAttempt {
  attempt_id: string
  generation: number
  cpu_limit_cores: number | null
  memory_limit_bytes: number | null
  throttled_seconds: number | null
  peak_throttled_share: number | null
  /** Throttled periods over all periods, for the attempt so far. */
  mean_throttled_share: number | null
  points: UtilisationPoint[]
}

export interface UtilisationRead {
  task_id: string
  read_at: string
  attempts: UtilisationAttempt[]
}

/**
 * WHAT "AT THE LIMIT" MEANS: a 5-second window whose mean use is at least
 * this share of the limit. Not 1.0: the CFS quota is enforced per 100 ms
 * period and the window is read from two cumulative counters, so a container
 * pinned at its quota for the whole window reads a hair under the limit as
 * often as on it. 95% is pinned for any reader of the chart; a window at 90%
 * had real headroom in some of its periods. A chosen value, not a measured
 * one: change it here and the summary line follows.
 */
export const AT_LIMIT_SHARE = 0.95

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * The route's body, read defensively: a field of the wrong type is null (not
 * measured), and a point with no date is dropped -- it has no position, and
 * the route never serves one.
 */
export function normUtilisation(raw: unknown): UtilisationRead {
  const body = (raw ?? {}) as Record<string, unknown>
  const attempts = Array.isArray(body.attempts) ? body.attempts : []
  return {
    task_id: typeof body.task_id === 'string' ? body.task_id : '',
    read_at: typeof body.read_at === 'string' ? body.read_at : '',
    attempts: attempts.map((a: Record<string, unknown>) => ({
      attempt_id: typeof a.attempt_id === 'string' ? a.attempt_id : '',
      generation: num(a.generation) ?? 0,
      cpu_limit_cores: num(a.cpu_limit_cores),
      memory_limit_bytes: num(a.memory_limit_bytes),
      throttled_seconds: num(a.throttled_seconds),
      peak_throttled_share: num(a.peak_throttled_share),
      mean_throttled_share: num(a.mean_throttled_share),
      points: (Array.isArray(a.points) ? a.points : [])
        .filter((p: Record<string, unknown>) => typeof p?.at === 'string')
        .map((p: Record<string, unknown>) => ({
          at: p.at as string,
          cpu_cores: num(p.cpu_cores),
          memory_bytes: num(p.memory_bytes),
          throttled_share: num(p.throttled_share),
        })),
    })),
  }
}

/** The figures the one-line summary says; each null when nothing measured it. */
export interface CpuSummary {
  mean: number | null
  limit: number | null
  /** Share (0-1) of the measured windows at the limit; null without a limit. */
  atLimit: number | null
  /** Share (0-1) of CFS periods throttled. */
  throttled: number | null
}

export function cpuSummary(a: UtilisationAttempt): CpuSummary {
  const cpu = a.points.map((p) => p.cpu_cores).filter((v): v is number => v !== null)
  const mean = cpu.length === 0 ? null : cpu.reduce((s, v) => s + v, 0) / cpu.length
  const limit = a.cpu_limit_cores
  const atLimit =
    limit === null || limit <= 0 || cpu.length === 0
      ? null
      : cpu.filter((v) => v >= limit * AT_LIMIT_SHARE).length / cpu.length
  // The attempt's own figure is periods over periods; the points' mean is the
  // fallback for a row whose heartbeats did not carry it yet.
  const thr = a.points.map((p) => p.throttled_share).filter((v): v is number => v !== null)
  const throttled = a.mean_throttled_share ?? (thr.length === 0 ? null : thr.reduce((s, v) => s + v, 0) / thr.length)
  return { mean, limit, atLimit, throttled }
}

function pct(share: number): string {
  return `${Math.round(share * 100)}%`
}

function coresText(n: number): string {
  return `${Number(n.toFixed(1))}`
}

/** "CPU: mean 1.7 of 4 cores · at the limit 22% of the time · throttled 9%". */
export function cpuSummaryText(s: CpuSummary): string {
  const mean =
    s.mean === null
      ? 'mean not measured'
      : s.limit === null
        ? `mean ${coresText(s.mean)} cores · limit not reported`
        : `mean ${coresText(s.mean)} of ${coresText(s.limit)} cores`
  const parts = [`CPU: ${mean}`]
  if (s.atLimit !== null) parts.push(`at the limit ${pct(s.atLimit)} of the time`)
  parts.push(s.throttled === null ? 'throttling not measured' : `throttled ${pct(s.throttled)}`)
  return parts.join(' · ')
}

/**
 * The windows the container was CPU-throttled, as [start, end] instants in ms.
 *
 * A point closes a window that began at the point before it, so a throttled
 * point marks the span from its predecessor to itself; touching spans merge.
 * The first point has no predecessor and is given the median spacing, which
 * is the sampler's 5 s. A point whose throttling was not read (null) is not
 * marked: "not measured" is not "not throttled", but it is not "throttled"
 * either, and the shading claims the latter.
 */
export function throttledSpans(points: readonly UtilisationPoint[]): Array<[number, number]> {
  const ts = points.map((p) => Date.parse(p.at))
  const gaps = ts.slice(1).map((t, i) => t - (ts[i] as number)).filter((g) => g > 0).sort((x, y) => x - y)
  const step = gaps.length === 0 ? 5_000 : (gaps[Math.floor(gaps.length / 2)] as number)
  const spans: Array<[number, number]> = []
  points.forEach((p, i) => {
    const end = ts[i] as number
    if (!Number.isFinite(end) || p.throttled_share === null || p.throttled_share <= 0) return
    const prev = i === 0 ? end - step : (ts[i - 1] as number)
    const start = Number.isFinite(prev) ? prev : end - step
    const last = spans[spans.length - 1]
    if (last !== undefined && start <= last[1]) last[1] = Math.max(last[1], end)
    else spans.push([start, end])
  })
  return spans
}
