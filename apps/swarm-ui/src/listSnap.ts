import { useSyncExternalStore } from 'react'

import { safeStorage } from './panes'

/**
 * THE AGENT LIST'S WIDTH BESIDE AN OPEN AGENT (agents.html V1, decided
 * 2026-10-01): the divider snaps the list to a 64px strip of state marks, the
 * 380px compact list, or half the work area, and the choice is remembered per
 * device.
 *
 * ONE STORE, TWO CONTROLS. The divider is drawn by the split (AgentSplit.tsx)
 * and the collapse toggle sits in the LIST's header (Agents.tsx), where V1
 * draws it -- two components with no parent in common below App. A tiny
 * external store lets both read and write one value without App threading a
 * prop through the shell, which is not this lane's to edit.
 *
 * `localStorage` THROWS in a private window; `safeStorage` (panes.ts) is the
 * guarded handle every pane preference here goes through.
 */
export type ListSnap = 'strip' | 'list' | 'half'

export const SNAPS: readonly ListSnap[] = ['strip', 'list', 'half']

export const SNAP_WIDTH: Readonly<Record<ListSnap, string>> = { strip: '64px', list: '380px', half: '50%' }

/** What a stop is called on the divider's label while it is dragged, as V1 prints it. */
export const SNAP_LABEL: Readonly<Record<ListSnap, string>> = {
  strip: 'strip · 64px',
  list: 'compact · 380px',
  half: 'wide · 50%',
}

/** The divider's `aria-valuetext`. */
export const SNAP_TEXT: Readonly<Record<ListSnap, string>> = {
  strip: 'list folded to a 64px strip',
  list: 'compact list, 380px',
  half: 'list at half the width',
}

export const LIST_SNAP_PREF = 'swarm.agents.list'

function stored(): ListSnap {
  try {
    const v = safeStorage()?.getItem(LIST_SNAP_PREF) ?? null
    return v === 'strip' || v === 'half' ? v : 'list'
  } catch {
    return 'list'
  }
}

let current: ListSnap | null = null
const listeners = new Set<() => void>()

/** The snap in force: the stored one until something sets another. */
export function listSnap(): ListSnap {
  if (current === null) current = stored()
  return current
}

/**
 * Set the snap. `remember` writes it to the device -- a click on the toggle,
 * an arrow key, a released drag -- and is false while a drag is still moving,
 * so a pass over the half stop on the way to the strip is not what is kept.
 */
export function setListSnap(next: ListSnap, remember = true): void {
  current = next
  if (remember) {
    try {
      safeStorage()?.setItem(LIST_SNAP_PREF, next)
    } catch {
      // A preference that cannot be written is a preference for this visit.
    }
  }
  for (const fn of listeners) fn()
}

/** Fold the list to the strip, or open it again to the compact list. */
export function toggleListSnap(): void {
  setListSnap(listSnap() === 'strip' ? 'list' : 'strip')
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function useListSnap(): ListSnap {
  return useSyncExternalStore(subscribe, listSnap, listSnap)
}

/** Forget the in-memory value, so the next read takes the stored one. For tests. */
export function resetListSnap(): void {
  current = null
  for (const fn of listeners) fn()
}

/** The stop nearest a pointer `x` pixels into a work area `w` pixels wide. */
export function nearestSnap(x: number, w: number): ListSnap {
  const stops: [ListSnap, number][] = [
    ['strip', 64],
    ['list', 380],
    ['half', w / 2],
  ]
  return stops.reduce((best, s) => (Math.abs(s[1] - x) < Math.abs(best[1] - x) ? s : best))[0]
}
