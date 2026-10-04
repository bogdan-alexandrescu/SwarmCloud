/**
 * AN ADDRESS THIS CONSOLE HAS NO PAGE FOR (browser QA U10b, owner 2026-10-04).
 *
 * `/capacity/quota` used to land on Overview with the bar rewritten, so a stale
 * or mistyped link opened a page that looked right and answered nothing -- the
 * reader could not tell the link was wrong. Now the content area says which
 * address has no page, the bar keeps it, and the page offers the nearest route
 * that exists, with Overview beside it. The spine and panel stay usable, as
 * states.html C draws a whole-page state.
 */
import type { MouseEvent } from 'react'

import { EmptyState } from './components'
import { FIXED } from './paths'

/** Every page with a fixed path, plus the agent lists, in the nav's order. */
const KNOWN: readonly string[] = [
  ...Object.values(FIXED),
  '/agents/live',
  '/agents/waiting',
  '/agents/recent',
]

function segmentsOf(path: string): string[] {
  return path.replace(/[?#].*$/, '').split('/').filter((s) => s !== '')
}

/** Whether every segment of `want` appears in `have`, in order. */
function within(want: readonly string[], have: readonly string[]): boolean {
  let i = 0
  for (const seg of have) if (seg === want[i]) i++
  return i === want.length
}

function editDistance(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    let diag = row[0]!
    row[0] = i
    for (let j = 1; j <= b.length; j++) {
      const up = row[j]!
      row[j] = Math.min(up + 1, row[j - 1]! + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1))
      diag = up
    }
  }
  return row[b.length]!
}

/**
 * The route an unknown path most likely meant. A known path that holds every
 * segment asked for, in order, under the same section, wins, the shortest first (`/capacity/quota` is
 * `/capacity/accounts/quota`); otherwise the closest spelling within a few
 * edits (`/agnets` is `/agents`); otherwise Overview.
 */
export function nearestPath(path: string): string {
  const want = segmentsOf(path)
  if (want.length === 0) return '/overview'
  // Same section first: `/workflow` is not `/submit/workflow`.
  const holding = KNOWN.filter((k) => segmentsOf(k)[0] === want[0] && within(want, segmentsOf(k)))
  if (holding.length > 0) {
    return holding.reduce((best, k) => (segmentsOf(k).length < segmentsOf(best).length ? k : best))
  }
  const asked = `/${want.join('/')}`
  const limit = Math.max(2, Math.floor(asked.length / 3))
  let best: { path: string; d: number } | null = null
  for (const k of KNOWN) {
    const d = editDistance(asked, k)
    if (d <= limit && (best === null || d < best.d)) best = { path: k, d }
  }
  return best?.path ?? '/overview'
}

export function NotFound({ path, go }: { path: string; go: (to: string) => void }) {
  const near = nearestPath(path)
  const follow = (to: string) => (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    go(to)
  }
  return (
    <div className="nf-page">
      <EmptyState kind="partial" heading={`No page at ${path}`}>
        This console has no page at that address, so nothing here is a reading of the platform. The nearest page that
        exists is{' '}
        <a className="mono" href={near} onClick={follow(near)}>
          {near}
        </a>
        {near === '/overview' ? '.' : (
          <>
            ; or go to{' '}
            <a href="/overview" onClick={follow('/overview')}>
              Overview
            </a>
            .
          </>
        )}
      </EmptyState>
    </div>
  )
}
