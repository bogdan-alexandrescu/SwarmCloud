import { useEffect, useState } from 'react'

import type { TaskPane } from '../App'
import type { LiveState } from '../LiveBrowser'

// WHICH PANE THE SPLIT OPENS ON (moved out of AgentSplit.tsx by SPEED-D4A,
// 2026-10-14, unchanged).

/*
 * A RUNNING AGENT OPENS ON LOGS, A FINISHED ONE ON DETAILS (owner decision
 * 2026-10-04). Decided once per agent, on the first read of its document,
 * and only when the address named no pane: a link to `/attempts` stays
 * there, and a reader who then picks Details is not sent back to Logs.
 * Until that read lands nothing is drawn in the pane, so a running agent's
 * Details are not read and thrown away on the way to its log.
 *
 * `/details` IS AN ADDRESS THAT NAMED A PANE (QA14-UI, owner QA
 * 2026-10-13): it sent a running agent to Browser or Logs, because Details
 * is written with no segment and so looked like no pane at all. The router
 * says when the address spelled it out, and that is honoured like `/attempts`.
 */
export function useOpeningPane({
  taskId,
  pane,
  detailNamed,
  headStatus,
  inFlight,
  liveRead,
  go,
  base,
}: {
  taskId: string
  pane: TaskPane
  detailNamed: boolean
  /** The status of the header's read of the task document. */
  headStatus: string
  inFlight: boolean
  liveRead: LiveState
  go: (to: string) => void
  base: string
}): boolean {
  const [decided, setDecided] = useState<string | null>(null)
  const defaulted = pane === 'detail' && !detailNamed
  const deciding = decided !== taskId && defaulted && (headStatus === 'loading' || inFlight)
  // BROWSER HAS ITS OWN ADDRESS NOW (LB-UX1, owner decision 2026-10-12):
  // `<agent>/browser`, so a running browser agent is sent there as a
  // running agent of any other profile is sent to `/logs`.
  useEffect(() => {
    if (decided === taskId) return
    if (!defaulted) {
      setDecided(taskId)
      return
    }
    if (headStatus === 'loading') return
    if (inFlight && liveRead.status === 'loading') return
    setDecided(taskId)
    if (inFlight && liveRead.status === 'ok' && liveRead.liveBrowser) go(`${base}/browser`)
    else if (inFlight) go(`${base}/logs`)
  }, [decided, taskId, defaulted, headStatus, inFlight, liveRead, go, base])
  return deciding
}
