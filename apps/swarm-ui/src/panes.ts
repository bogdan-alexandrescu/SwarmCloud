/**
 * The shell's arithmetic, with no DOM in it.
 *
 * WHY THIS FILE EXISTS. §B3 gives the inspector and the dock a width a viewer
 * sets by dragging and the browser remembers. Two things there are easy to get
 * wrong and impossible to see once they are wrong: the clamp (a pane dragged
 * to 12px is a pane nobody can drag back) and the storage read (a private
 * window throws on `localStorage`, and a `catch` that returns `undefined`
 * renders a pane of width `NaN`). Both are pure functions here so they are
 * tested without a browser, which is what `make test` can run.
 *
 * THE PERCENTILE IS HERE FOR A DIFFERENT REASON. §B18 collapses the
 * sixteen-card telemetry strip to one line carrying a count and a p95. A p95
 * is a claim about a distribution, and the distribution this app actually
 * holds is ONE sample per route -- the most recent attempt. Saying "p95" over
 * sixteen samples without saying which sixteen is the same class of lie as a
 * count computed over a partial read, so the label that renders it says so and
 * the function is named for what it does rather than for what it sounds like.
 */

import type { ProbeRecord } from './fetch'

// ---------------------------------------------------------------------------
// Pane widths
// ---------------------------------------------------------------------------

export interface PaneSpec {
  /** The `localStorage` key. Namespaced, because an artifact origin is shared. */
  key: string
  min: number
  max: number
  initial: number
}

/** §B3: "right pane, 480px default, resizable 400-720px". */
export const INSPECTOR: PaneSpec = {
  key: 'swarm.inspector.w',
  min: 400,
  max: 720,
  initial: 480,
}

/**
 * §B3: "bottom, 28px collapsed, expands to 240px, resizable 120px-70vh".
 *
 * `max` is a fallback only: the real ceiling is 70% of the viewport and is
 * computed at the call site, because a 70vh written here would be a second
 * definition of the same rule that could not see the window.
 */
export const DOCK: PaneSpec = {
  key: 'swarm.dock.h',
  min: 120,
  max: 640,
  initial: 240,
}

/** §B3: the dock's collapsed height. One line, and it is always present. */
export const DOCK_COLLAPSED = 28

/**
 * A pane size, forced into range.
 *
 * NON-FINITE IS THE CASE THAT MATTERS. `Number.parseInt('')` is `NaN`, and
 * `Math.min(Math.max(NaN, min), max)` is `NaN` -- which reaches the DOM as
 * `width: NaNpx`, which the browser drops, which renders the pane at its
 * content width with no way back. So it is checked first and answers `min`,
 * never the raw input.
 */
export function clampPane(px: number, min: number, max: number): number {
  if (!Number.isFinite(px)) return min
  if (max < min) return min
  return Math.min(Math.max(Math.round(px), min), max)
}

/**
 * A remembered size, or the default.
 *
 * Every access is wrapped: `localStorage` is a getter that THROWS in a private
 * window and when site data is blocked, so an unguarded read takes the whole
 * shell down rather than losing one preference.
 */
export function readPane(spec: PaneSpec, storage: Pick<Storage, 'getItem'> | null = safeStorage()): number {
  if (storage === null) return spec.initial
  try {
    const raw = storage.getItem(spec.key)
    if (raw === null) return spec.initial
    const n = Number.parseInt(raw, 10)
    if (!Number.isFinite(n)) return spec.initial
    return clampPane(n, spec.min, spec.max)
  } catch {
    return spec.initial
  }
}

/** Remember a size. A failure here loses a preference and nothing else. */
export function writePane(
  spec: PaneSpec,
  px: number,
  storage: Pick<Storage, 'setItem'> | null = safeStorage(),
): void {
  if (storage === null) return
  try {
    storage.setItem(spec.key, String(clampPane(px, spec.min, spec.max)))
  } catch {
    /* A viewer with storage blocked keeps the default. That is the whole cost. */
  }
}

/** `localStorage`, or null where touching it throws. */
export function safeStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// The telemetry summary (§B18)
// ---------------------------------------------------------------------------

/**
 * The nearest-rank percentile of a sample, or null when there is no sample.
 *
 * Nearest-rank rather than an interpolated one: with sixteen values an
 * interpolation invents a latency that no request had, and every number on
 * these screens has to be one something actually measured.
 */
export function percentile(values: readonly number[], p: number): number | null {
  const sorted = values.filter((v) => Number.isFinite(v)).slice().sort((a, b) => a - b)
  if (sorted.length === 0) return null
  const rank = Math.ceil((p / 100) * sorted.length)
  const index = Math.min(sorted.length - 1, Math.max(0, rank - 1))
  return sorted[index] ?? null
}

export interface ProbeSummary {
  /** Routes called in this tab. Not requests: one record per route. */
  routes: number
  /** Routes whose last attempt failed, excluding the admin gate. */
  failed: number
  /** Routes that answered 403 because the caller is not an admin. */
  adminOnly: number
  /** p95 over the LAST attempt of each route. Null when nothing was called. */
  p95Ms: number | null
  /** The newest successful payload of any route, or null if none ever landed. */
  newestSuccessAt: number | null
  /** A session that has expired is an action, not a statistic. */
  expired: boolean
}

/**
 * The strip, in one line's worth of facts.
 *
 * `admin_required` is counted apart from `failed` deliberately and for the
 * reason the strip has always carried: a non-admin genuinely cannot read
 * `/v1/admin/*`, and folding that into a failure count is what makes a working
 * console report itself broken.
 */
export function summariseProbes(probes: readonly ProbeRecord[]): ProbeSummary {
  let failed = 0
  let adminOnly = 0
  let expired = false
  let newestSuccessAt: number | null = null
  const latencies: number[] = []

  for (const p of probes) {
    latencies.push(p.lastLatencyMs)
    if (p.lastKind === 'admin_required') adminOnly += 1
    else if (p.lastKind !== null) failed += 1
    if (p.lastKind === 'unauthenticated' || p.lastKind === 'session_expired') expired = true
    if (p.lastSuccessAt !== null && (newestSuccessAt === null || p.lastSuccessAt > newestSuccessAt)) {
      newestSuccessAt = p.lastSuccessAt
    }
  }

  return {
    routes: probes.length,
    failed,
    adminOnly,
    p95Ms: percentile(latencies, 95),
    newestSuccessAt,
    expired,
  }
}
