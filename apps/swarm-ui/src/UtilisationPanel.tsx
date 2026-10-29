// Details' CPU and memory over time, read on its own clock (LIVE-UTIL-UI;
// part of #1689).
//
// WHY ITS OWN READ AND NOT ONE MORE IN `loadAgentRun`. The utilisation route
// reads up to 2000 HEARTBEAT events per call (`swarm_api.utilisation`); folded
// into the drawer's read it would ride along on every screen that loads a
// run, and a slow or failed read would hold up the whole Details tab. Here it
// is read only where it is drawn, and only its own chart waits for it.
//
// THE CADENCE. While the task runs, every `UTILISATION_POLL_MS`; when it
// finishes, ONE more read -- the worker's last heartbeat carries the last
// points, and it lands at the finish -- and then none: a finished attempt's
// series does not change, and polling it is spend with nothing to learn.

import { useEffect, useRef, useState } from 'react'

import { loadUtilisation } from './api'
import { UtilisationChart, utilisationHeadline } from './charts/Utilisation'
import type { Result } from './fetch'
import { cpuSummary, type UtilisationRead } from './utilisation'

/**
 * How often a running task's series is read: the drawer's own cadence
 * (`DRAWER_POLL_MS` in AgentDetail.tsx, which this module cannot import
 * without a cycle), so the chart moves with the figures beside it. Faster buys
 * nothing: points arrive only with a heartbeat, every 60-150 s, and each read
 * costs the route an events query.
 */
export const UTILISATION_POLL_MS = 10_000

export function UtilisationPanel({
  taskId,
  live,
}: {
  taskId: string
  live: boolean
}) {
  const [got, setGot] = useState<Result<UtilisationRead> | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    let cancelled = false
    const tick = async (first: boolean) => {
      // One read at a time: a read slower than the cadence does not stack.
      // The first read of an effect always goes, so the read at the finish is
      // never skipped for a poll still in flight from the running task.
      if (inFlight.current && !first) return
      inFlight.current = true
      try {
        const r = await loadUtilisation(taskId)
        if (!cancelled) setGot(r)
      } finally {
        inFlight.current = false
      }
    }
    void tick(true)
    const id = live ? setInterval(() => void tick(false), UTILISATION_POLL_MS) : null
    return () => {
      cancelled = true
      if (id !== null) clearInterval(id)
    }
  }, [taskId, live])

  // A read that has not landed, or that failed, is a sentence in the card,
  // never a chart: there is nothing to fold away.
  if (got === null || got.status === 'loading') {
    return (
      <p className="dt-note" role="status">
        CPU and memory over time: reading
      </p>
    )
  }
  if (got.status === 'error') {
    return (
      <p className="dt-note" role="status" data-testid="util-unread">
        CPU and memory over time: not read ({got.error.message})
      </p>
    )
  }
  const attempt = got.status === 'empty' ? null : (got.data.attempts[0] ?? null)
  const head = utilisationHeadline(attempt, live)
  // ONE MEAN, ONE LIMIT, A SHORT TITLE (QA1 overview-agents-24/25,
  // 2026-10-28): the fold's title wrapped to three lines and printed a second
  // mean of a second limit (`mean 2.8 of 4 cores (the limit the 5 s samples
  // carry; the attempt's cgroup limit is 2 cores)`) under the Resources
  // rows' `mean 0.8 … of 2 cores`. The title says what the chart adds -- time
  // at the limit and throttling -- and the samples' mean stays in the chart's
  // own caption, opened.
  const s = attempt === null ? null : cpuSummary(attempt)
  const headline =
    head.measured && s !== null
      ? [
          s.atLimit === null ? null : `at the limit ${Math.round(s.atLimit * 100)}% of the time`,
          s.throttled === null ? 'throttling not measured' : `throttled ${Math.round(s.throttled * 100)}%`,
        ]
          .filter((p): p is string => p !== null)
          .join(' · ')
      : head.text
  // THE SUMMARY ON THE GLASS, THE CHART ONE CLICK AWAY. Details keeps every
  // chart behind a disclosure so its first 900px stay Now, the strip and the
  // column heads (details.v3.test.tsx). Time at the limit and throttled are
  // the disclosure's own summary, so they are read without opening anything,
  // and live; the mean of the limit is the chart's caption, inside.
  return (
    <details className="dt-disc ctl-util-disc">
      <summary data-testid="util-headline">CPU and memory over time · {headline}</summary>
      <UtilisationChart attempt={attempt} live={live} />
      {got.status === 'stale' && (
        <p className="ctl-chart-note" role="status">
          the last read failed ({got.error.message}); this is the one before it
        </p>
      )}
    </details>
  )
}
