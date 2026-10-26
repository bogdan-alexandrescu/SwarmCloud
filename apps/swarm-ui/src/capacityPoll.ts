import { useSyncExternalStore } from 'react'
import { isPaused } from './fetch'
import { phoneWidth } from './HelpCard'
import { POOL_FAMILY_ORDER, overCeiling, poolKind, type Capacity, type Pool } from './types'

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
 * ONE PARAMETER OF THE ADDRESS (#125, #128; QA G5-23): the hash grammar's
 * query first, then the real path's query, which is where the router leaves
 * it once it has rewritten the hash -- the rule Pool limits reads its own
 * `?pool=` by (AdminSettings `linkedPool`). Followed through `hashchange` and
 * `popstate`, so a second link followed while the screen is mounted moves it.
 */
export function linkedParam(name: string): string | null {
  if (typeof window === 'undefined') return null
  const hash = window.location.hash
  const at = hash.indexOf('?')
  if (at !== -1) return new URLSearchParams(hash.slice(at + 1)).get(name)
  return new URLSearchParams(window.location.search).get(name)
}

/**
 * THE POOL A LINK NAMED, read off the address (#125, #128): Pools' row links
 * to `#capacity/holders?pool=<name>`, Provider quota's Feeds pool to
 * `#capacity/pools?pool=<name>`.
 */
export function linkedPool(): string | null {
  return linkedParam('pool')
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

/**
 * Any one parameter of the address, followed as `useLinkedPool` follows
 * `pool`: Holders' `tenant`, Accounts' `account` (QA G5-23: neither the
 * tenant chip nor the chosen account could be linked to).
 */
export function useLinkedParam(name: string): string | null {
  return useSyncExternalStore(subscribeAddress, () => linkedParam(name), () => null)
}

/**
 * A capacity pane's address with its query: `#capacity/holders?pool=x&tenant=eng`.
 * Empty and null values are left out, so clearing the last one gives the bare
 * pane. Written to `location.hash`, which the router reads, canonicalises
 * (App.tsx keeps exactly these keys) and moves into the path.
 */
export function paneHref(address: string, params: Record<string, string | null | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') q.set(k, v)
  const query = q.toString()
  return query === '' ? `#${address}` : `#${address}?${query}`
}

/** The in-app address of one pool's row on Pools, Holders or Pool limits. */
export function poolHref(address: 'capacity/pools' | 'capacity/holders' | 'admin/limits', pool: string): string {
  return `#${address}?pool=${encodeURIComponent(pool)}`
}

/**
 * PHONE TABLES STACK (QA G5-08, 2026-10-07). At 390 every capacity and admin
 * table scrolled sideways and hid its answer column -- Pools' Use and State,
 * the profile matrix's Can start, Holders' Heartbeat, Pool limits' `edit` cut
 * to `edi`, a 1,785px Tenants roster in a 356px box -- while Holders' drift
 * table, the Runtimes cards and Accounts already stacked. Below the phone
 * breakpoint (`phoneWidth`, 560px) a table here is the `data-label` record the
 * drift table draws (`.is-stacked`, styles.css §B6.3); above it, CH-13's
 * scroll with the name held (`.is-scroll`), which is right where most of the
 * row fits. The stacked rules are a <=899px block, which is why the switch is
 * here and not in the sheet: rendered only at <=560px, the class is the
 * phone's alone.
 */
export function usePhoneTables(): boolean {
  return useSyncExternalStore(subscribePhone, phoneWidth, () => false)
}

/** The wrapper class a capacity table takes at this width: see `usePhoneTables`. */
export function tableMode(phone: boolean): 'is-stacked' | 'is-scroll' {
  return phone ? 'is-stacked' : 'is-scroll'
}

function subscribePhone(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia('(max-width: 560px)')
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

/**
 * The tenant a capacity read is scoped to: whose pool a row's `this tenant`
 * means, and whose pools `comparePools` puts first. Moved here from
 * Capacity.tsx so Pool limits reads the same tenant (QA G5-22).
 *
 * `/v1/capacity` names the tenant itself -- service.capacity() has always
 * sent `tenant_id`, and Pools used to guess it back out of the pool names.
 * The regex stays as the fallback for an API that predates the field,
 * because a blank here silently drops the scope from every figure.
 */
export function poolViewer(capacity: Pick<Capacity, 'tenant_id' | 'pools'>): string | undefined {
  return (
    capacity.tenant_id ??
    capacity.pools
      .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
      .find((t): t is string => Boolean(t))
  )
}

/**
 * NEAR ITS CEILING: this share of it in use, or more. Pool limits' rule
 * (AdminSettings `NEAR_CEILING`, its lead and the panel's `near ceiling`
 * count), restated here only because AdminSettings imports this module and
 * the import cannot run the other way; `vq.capacity.pools.test.tsx` holds the
 * two equal. Pools draws a pool past it `near ceiling` in the full mark's
 * amber (VQ-031): `anthropic · eng` at 46 of 50 read `ok` in the same grey as
 * an idle pool.
 */
export const POOL_NEAR_CEILING = 0.8

/** True when a pool is not full but holds `POOL_NEAR_CEILING` of its ceiling or more. */
export function nearCeiling(p: Pool): boolean {
  const limit = p.effective_limit
  if (overCeiling(p) || isPaused(p) || limit === null || limit <= 0 || p.active >= limit) return false
  return p.active / limit >= POOL_NEAR_CEILING
}

/** True when something -- AIMD or a provider quota -- holds a pool's ceiling under its configured hard limit. */
export function loweredCeiling(p: Pool): boolean {
  return p.effective_limit !== null && p.hard_limit !== null && p.effective_limit > 0 && p.effective_limit < p.hard_limit
}

/**
 * How abnormal a pool is, worst first (#125): over its ceiling, paused, no
 * limit set or limit 0, full, near its ceiling, lowered -- the order Pools
 * draws its marks in -- and 6 for a pool with nothing to look at. Near and
 * lowered rank too (VQ-031): the four lowered pools sat at the foot of
 * Providers, under every idle slice.
 */
export function poolProblemRank(p: Pool): number {
  const limit = p.effective_limit
  if (overCeiling(p)) return 0
  if (isPaused(p)) return 1
  if (limit === null || limit === 0) return 2
  if (p.active >= limit) return 3
  if (nearCeiling(p)) return 4
  if (loweredCeiling(p)) return 5
  return 6
}

/** True when a pool is this tenant's: `tenant:<viewer>` or `<anything>:tenant:<viewer>`. */
function viewersPool(name: string, viewer: string | undefined): boolean {
  if (viewer === undefined) return false
  return /(?:^|:)tenant:([^:]+)$/.exec(name)?.[1] === viewer
}

/**
 * ONE POOL ORDER (QA G5-22, 2026-10-07). Pools sorted families by use, Pool
 * limits alphabetically, Holders' drift table by count, so the same pool sat
 * at a different place on each screen and a reader moving between them had to
 * search for it again. Every pool list on Pools and Pool limits now reads:
 * family (`POOL_FAMILY_ORDER`), then problems first (#125's rank, so a full
 * or paused pool still heads its family), then this tenant's own, then name.
 *
 * NOT BY USE ANY MORE. #125 sorted a healthy family by used/ceiling, highest
 * first; a 30-second poll then reordered rows under the reader whenever two
 * pools crossed, and Pool limits -- an editing screen -- could not share it.
 * The rank keeps what #125 was for: the abnormal row is at the top.
 */
export function comparePools(viewer: string | undefined): (a: Pool, b: Pool) => number {
  const family = (p: Pool) => {
    const i = POOL_FAMILY_ORDER.indexOf(poolKind(p.name))
    return i === -1 ? POOL_FAMILY_ORDER.length : i
  }
  const mine = (p: Pool) => (viewersPool(p.name, viewer) ? 0 : 1)
  return (a, b) =>
    family(a) - family(b) ||
    poolProblemRank(a) - poolProblemRank(b) ||
    mine(a) - mine(b) ||
    a.name.localeCompare(b.name)
}
