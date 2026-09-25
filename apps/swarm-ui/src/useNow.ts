import { useCallback, useSyncExternalStore } from 'react'

/**
 * ONE CLOCK PER CADENCE, SHARED BY EVERY COMPONENT THAT READS IT.
 *
 * THE DEFECT (CH-1, AG-2). Every age on these screens was computed from
 * `Date.now()` at render, and most of them had nothing to re-render them: the
 * provenance line under a screen title said `read just now` beside a head that
 * said `newest read 22s ago`, and a screen left open overnight still said
 * `just now` in the morning. The inspector took its clock once, so `run`,
 * `Elapsed` and `live 36s ago` never moved. Where a component DID tick -- the
 * head, the dock, the API reads page -- it held its own `setInterval`, so two
 * ages side by side were computed at two different instants and could
 * disagree by up to a whole interval.
 *
 * SHARED, NOT MERELY REUSED. Every caller asking for the same cadence reads
 * the same instant from one interval, so the head, the dock and a screen's
 * sub-line all move on the same tick and cannot contradict each other. A
 * hundred rows on a 1s clock are one interval, not a hundred.
 *
 * WHAT IT IS NOT. It re-renders; it does not re-read. A ticking age over data
 * nobody re-fetched is still a claim about the past, and `Screen` pairs this
 * with its own stale treatment (and `pollMs`) for exactly that reason. A
 * clock alone on a live view slides into `silent` against events that are
 * simply old -- AG-2's warning, and why the inspector also re-reads.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect`, because the
 * value is module state that several components share; the server snapshot is
 * the same instant, so `renderToStaticMarkup` (tests/run.mjs) reads a number
 * and starts no timer.
 */

interface Clock {
  /** The instant of the last tick, or of the clock's creation. */
  now: number
  readonly listeners: Set<() => void>
  timer: ReturnType<typeof setInterval> | null
}

const clocks = new Map<number, Clock>()

function clockFor(intervalMs: number): Clock {
  let clock = clocks.get(intervalMs)
  if (clock === undefined) {
    clock = { now: Date.now(), listeners: new Set(), timer: null }
    clocks.set(intervalMs, clock)
  }
  return clock
}

function subscribe(intervalMs: number, onTick: () => void): () => void {
  const clock = clockFor(intervalMs)
  clock.listeners.add(onTick)
  if (clock.timer === null) {
    // AN IDLE CLOCK IS BROUGHT TO NOW BEFORE ANYONE RELIES ON IT. A render that
    // never committed can leave a snapshot behind with no timer to move it;
    // React reads the snapshot again after subscribing and re-renders if this
    // changed it, so the first paint after a mount is never an old instant.
    clock.now = Date.now()
    const tick = () => {
      clock.now = Date.now()
      for (const listener of clock.listeners) listener()
    }
    clock.timer = setInterval(tick, intervalMs)
  }
  return () => {
    clock.listeners.delete(onTick)
    if (clock.listeners.size === 0) {
      if (clock.timer !== null) clearInterval(clock.timer)
      clock.timer = null
      // Forgotten rather than kept idle, so the next mount starts from now.
      clocks.delete(intervalMs)
    }
  }
}

/**
 * The tick every AGE in the frame moves on: every screen's sub-line, the
 * head's `newest read`, the dock and the API reads page.
 *
 * ONE CONSTANT, NAMED BY EVERY CALLER. Clocks are shared per cadence, so two
 * callers share an instant only while they pass the same number. Shell.tsx
 * held `AGE_TICK_MS = 5_000` while the head, the API reads page and the dock
 * each passed their own `5000`: change the constant alone and the sub-line and
 * the head would move onto different clocks without a line of either
 * changing, which is CH-1 back. So the number lives here, beside the clock it
 * selects, and the age callers import it.
 */
export const AGE_TICK_MS = 5_000

/**
 * The current instant, re-rendering the caller every `intervalMs`.
 *
 * `AGE_TICK_MS` BY DEFAULT because that is the cadence every AGE on these
 * screens moves at, and a shared default is what keeps them on one tick. A
 * running DURATION (Agents' elapsed column, the inspector's `run`) wants 1000
 * and asks for it.
 */
export function useNow(intervalMs: number = AGE_TICK_MS): number {
  const sub = useCallback((onTick: () => void) => subscribe(intervalMs, onTick), [intervalMs])
  const snapshot = useCallback(() => clockFor(intervalMs).now, [intervalMs])
  return useSyncExternalStore(sub, snapshot, snapshot)
}
