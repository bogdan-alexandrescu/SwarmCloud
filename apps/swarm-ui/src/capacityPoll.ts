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
