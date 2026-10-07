import { useSyncExternalStore } from 'react'
import { phoneWidth } from './HelpCard'

/**
 * How often each Capacity screen re-reads (#117, owner ruling 2026-10-07):
 * Pools (Ceilings and By runner profile), Holders, Accounts and Provider
 * quota every 30 seconds on a desktop, and every 60 below the phone
 * breakpoint (`CAPACITY_PHONE_POLL_MS`). Every one of them reads through
 * `Screen` (Shell.tsx), which pauses the timer while `document.hidden` is
 * true, reads at once when the tab comes back, and stops after
 * `IDLE_STOP_MS` without input behind a `Paused · resume` control.
 *
 * The 2026-10-01 decision had Accounts at 60 s and Provider quota unpolled;
 * the 2026-10-07 ruling puts all four on one cadence, so a reader comparing a
 * pool's headroom with the accounts and quota behind it is comparing reads of
 * the same age.
 */
export const POOLS_POLL_MS = 30_000
export const HOLDERS_POLL_MS = 30_000
export const ACCOUNTS_POLL_MS = 30_000
export const QUOTA_POLL_MS = 30_000

/**
 * The same screens below the phone breakpoint (`phoneWidth`, HelpCard.tsx):
 * every 60 seconds (#117, owner ruling 2026-10-07). A phone on cellular pays
 * for every read in battery and data, and its reader is glancing, not
 * watching a pool drain.
 */
export const CAPACITY_PHONE_POLL_MS = 60_000

/**
 * A capacity screen's `pollMs`: `desktopMs` on a desktop, and
 * `CAPACITY_PHONE_POLL_MS` below the phone breakpoint. A function, so the
 * width is asked at each read (`Screen` plans every read through it) and a
 * rotated or resized window takes the right cadence on its next read.
 */
export function capacityPoll(desktopMs: number): (data: unknown) => number {
  return () => (phoneWidth() ? CAPACITY_PHONE_POLL_MS : desktopMs)
}

/**
 * THE POOL A LINK NAMED, read off the address (#125, #128): Pools' row links
 * to `#capacity/holders?pool=<name>`, Provider quota's Feeds pool to
 * `#capacity/pools?pool=<name>`. The hash grammar first, then the real path's
 * query, which is where the router leaves it once it has rewritten the hash --
 * the rule Pool limits reads its own `?pool=` by (AdminSettings `linkedPool`).
 * Followed through `hashchange` and `popstate`, so a second link followed while
 * the screen is mounted moves the mark.
 */
export function linkedPool(): string | null {
  if (typeof window === 'undefined') return null
  const hash = window.location.hash
  const at = hash.indexOf('?')
  if (at !== -1) return new URLSearchParams(hash.slice(at + 1)).get('pool')
  return new URLSearchParams(window.location.search).get('pool')
}

function subscribeAddress(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener('hashchange', onChange)
  window.addEventListener('popstate', onChange)
  return () => {
    window.removeEventListener('hashchange', onChange)
    window.removeEventListener('popstate', onChange)
  }
}

export function useLinkedPool(): string | null {
  return useSyncExternalStore(subscribeAddress, linkedPool, () => null)
}

/** The in-app address of one pool's row on Pools, Holders or Pool limits. */
export function poolHref(address: 'capacity/pools' | 'capacity/holders' | 'admin/limits', pool: string): string {
  return `#${address}?pool=${encodeURIComponent(pool)}`
}
