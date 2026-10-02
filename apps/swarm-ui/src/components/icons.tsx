/**
 * The handful of 24px stroke icons the canonical components draw themselves:
 * a chevron for an expandable row, the sort arrows, a close cross, the check
 * a toast acknowledges with. The shell's section icons are `Icon` in
 * Spine.tsx; these are the components' own so that a component never imports
 * the shell.
 */
import type { ReactNode } from 'react'

const PATHS: Readonly<Record<CIconName, ReactNode>> = {
  chevron: <path d="m9 6 6 6-6 6" />,
  up: <path d="M12 19V5M6 11l6-6 6 6" />,
  down: <path d="M12 5v14M6 13l6 6 6-6" />,
  sort: <path d="M8 9l4-4 4 4M8 15l4 4 4-4" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  check: <path d="M5 12.5 10 17l9-10" />,
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5" />
    </>
  ),
  out: <path d="M14 5h5v5M19 5l-8 8M18 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h4" />,
  swap: <path d="M7 7h11l-3-3M17 17H6l3 3" />,
}

export type CIconName = 'chevron' | 'up' | 'down' | 'sort' | 'close' | 'check' | 'copy' | 'out' | 'swap'

export function CIcon({ name, className = 'c-ic' }: { name: CIconName; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  )
}
