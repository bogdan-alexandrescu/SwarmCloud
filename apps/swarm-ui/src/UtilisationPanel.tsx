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
import { cpuSummary, type UtilisationAttempt, type UtilisationRead } from './utilisation'

/**
 * THE HEADLINE WITH ITS LIMIT NAMED (c05, owner decision 2026-10-22, box
 * comment 6298220484: "Name every count where it is printed"). `of 2 cores`
 * is the attempt's cgroup CPU limit -- what the kernel enforced, not the
 * class's guaranteed size the head prints -- so the line says which it is.
 * The words are added once, after the mean's `of N cores`, and only when the
 * route served a limit: `limit not reported` already says there is none.
 */
function namedLimit(attempt: UtilisationAttempt | null, text: string): string {
  const sampled = namedMean(text)
  if (attempt === null || cpuSummary(attempt).limit === null) return sampled
  return sampled.replace(/^(CPU, 5 s samples: mean \S+ of \S+ cores)/, '$1 (cgroup limit)')
}

/**
 * AND WHICH MEAN IT IS (FF-D5a, the same c05 box). Resources, above this
 * line, prints the attempt's own mean -- its CPU-seconds over the time its
 * runners ran -- and this one is the mean of the series' 5 s samples, setup
 * and waits included. Two means on one pane, each named where it is printed,
 * so a reader does not take the difference for an error.
 */
function namedMean(text: string): string {
  return text.replace(/^CPU: mean (?=\d)/, 'CPU, 5 s samples: mean ')
}

/**
 * How often a running task's series is read: the drawer's own cadence
 * (`DRAWER_POLL_MS` in AgentDetail.tsx, which this module cannot import
 * without a cycle), so the chart moves with the figures beside it. Faster buys
 * nothing: points arrive only with a heartbeat, every 60-150 s, and each read
 * costs the route an events query.
 */
export const UTILISATION_POLL_MS = 10_000

export function UtilisationPanel({ taskId, live }: { taskId: string; live: boolean }) {
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
  const headline = head.measured ? namedLimit(attempt, head.text) : head.text
  // THE SUMMARY ON THE GLASS, THE CHART ONE CLICK AWAY. Details keeps every
  // chart behind a disclosure so its first 900px stay Now, the strip and the
  // column heads (details.v3.test.tsx). The one line the owner asked for --
  // mean of the limit, time at the limit, throttled -- is the disclosure's
  // own summary, so it is read without opening anything, and live.
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
