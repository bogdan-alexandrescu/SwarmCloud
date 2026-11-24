import type { MouseEvent, ReactNode } from 'react'
import { addressToPath } from './paths'

/** A link inside the app: an href for a new tab, `go` for a plain click. */
export function InApp({
  to,
  go,
  children,
  className,
  current = false,
  onGo,
}: {
  to: string
  go: (to: string) => void
  children: ReactNode
  className?: string
  /** The link to the item this page has open: `aria-current`, so the choice is not colour alone. */
  current?: boolean
  /** Called before a plain click goes. */
  onGo?: () => void
}) {
  return (
    <a
      href={addressToPath(to)}
      className={className}
      aria-current={current ? 'true' : undefined}
      onClick={(e: MouseEvent) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        onGo?.()
        go(to)
      }}
    >
      {children}
    </a>
  )
}
