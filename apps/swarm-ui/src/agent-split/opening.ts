import { useEffect, useState } from 'react'

import type { TaskPane } from '../App'
import type { LiveState } from '../LiveBrowser'

// WHICH PANE THE SPLIT OPENS ON (moved out of AgentSplit.tsx by SPEED-D4A,
// 2026-10-14, unchanged).

/*
 * A RUNNING AGENT OPENS ON DETAILS, A RUNNING LIVE-BROWSER AGENT ON BROWSER
 * (OPEN-DETAILS, owner decision 2026-10-19, #1646: "Details, except live
 * browser"). A finished agent opens on Details, as before.
 *
 * History, one line each:
 * - 2026-10-04: a running agent opened on Logs; #1646 replaced that rule.
 * - LB-UX1, 2026-10-12: a running live-browser agent opens on `/browser`; kept.
 *
 * Decided once per agent, and only when the address named no pane: a link
 * to `/logs` or `/attempts` stays there, and a reader who then picks another
 * tab is not sent back. Until the decision lands nothing is drawn in the
 * pane, so a browser agent's Details are not read and thrown away on the way
 * to its Browser tab. A running agent that is not a live browser now stays
 * where it is, but the decision still waits on the live read: until that
 * read lands, nothing says the agent is not a live browser.
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
  // BROWSER HAS ITS OWN ADDRESS (LB-UX1, owner decision 2026-10-12):
  // `<agent>/browser`, where a running browser agent is sent; a running
  // agent of any other profile stays on Details (#1646).
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
  }, [decided, taskId, defaulted, headStatus, inFlight, liveRead, go, base])
  return deciding
}
