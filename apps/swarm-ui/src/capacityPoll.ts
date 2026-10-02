import { useSyncExternalStore } from 'react'

/**
 * How often each Capacity screen re-reads, decided on 2026-10-01 (#117,
 * capacity.html §G): Pools (Ceilings and By runner profile) and Holders every
 * 30 seconds, Accounts every 60. Every one of them reads through `Screen`
 * (Shell.tsx), which pauses the timer while `document.hidden` is true and
 * reads at once when the tab comes back, so a background tab polls nothing.
 *
 * Accounts is slower because its figures are a subscription's usage windows,
 * which the broker itself refreshes on a minutes scale; a 30s read would
 * mostly re-draw the same reading. Provider quota and Runtimes are not polled:
 * the decision named the three above and no others.
 */
export const POOLS_POLL_MS = 30_000
export const HOLDERS_POLL_MS = 30_000
export const ACCOUNTS_POLL_MS = 60_000

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
