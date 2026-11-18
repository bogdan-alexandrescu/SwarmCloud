import type { ReactNode } from 'react'

import { DRAWER_POLL_MS, SplitClock } from '../AgentDetail'
import { rowClock, useNow } from '../useNow'

// THE SPLIT'S CLOCK (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged).

/**
 * THE SPLIT'S ONE INSTANT (G2-03, QA 2026-10-07). One running task showed
 * `17m 33s` in the header and `17m 39s` on the Elapsed tile: one 1s clock,
 * capped by two different reads (the header's and the Details pane's), so
 * the two figures stopped at different instants. It is taken once here --
 * ticking every second, and stopping one poll past the header's read
 * (`rowClock`), so a failed re-read never ages a live agent -- and the
 * header and the Details tab both read it (`SplitClock`).
 *
 * A COMPONENT OF ITS OWN so the tick re-renders only what reads the clock:
 * its children are the split's elements, unchanged between ticks, so React
 * keeps them and only the context's readers draw again. Null until the
 * header's read lands, so the Details tab keeps its own clock until then.
 */
export function SplitTick({ live, readAt, children }: { live: boolean; readAt: number | null; children: ReactNode }) {
  const clock = useNow(1000)
  const now = readAt === null ? null : live ? rowClock(clock, readAt, DRAWER_POLL_MS) : clock
  return <SplitClock.Provider value={now}>{children}</SplitClock.Provider>
}
