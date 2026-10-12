import { useEffect, useRef, useState, type ReactNode } from 'react'

import { usePhoneTables } from './capacityPoll'
import { buttonClass } from './components'

/**
 * THE BROWSER TAB'S "⋯" MENU ON A PHONE (docs/design/live-browser.md §6, "On
 * the person's own phone": "the device switcher sits under the tab's ⋯
 * menu"; lane LB-UX3, part of #1030).
 *
 * At phone width (the two-page layout's 560px, `usePhoneTables`) the device
 * chips, rotate, touch and the logs toggle do not fit the toolbar beside ⤢,
 * so they fold into one "⋯" button whose menu holds them, unchanged: the
 * same components, so who may switch the device is still decided in one
 * place (`LiveDevice`). Wider, it draws its children where it stands, and
 * the toolbar is the desktop's.
 *
 * A SEPARATE FILE so LiveBrowser.tsx (lane UX1's) only wires it in.
 */
export function LiveToolMenu({ children }: { children: ReactNode }) {
  const phone = usePhoneTables()
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLSpanElement>(null)

  // Closed by a press outside it, as any menu is; Escape is handled on the menu.
  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => {
      if (box.current !== null && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])

  if (!phone) return <>{children}</>
  return (
    <span className="live-more" ref={box}>
      <button
        type="button"
        className={buttonClass({ size: 'sm', iconOnly: true }, false, 'live-more-btn')}
        aria-label="Browser view options"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        ⋯
      </button>
      {open && (
        <div
          className="live-more-menu"
          role="group"
          aria-label="Browser view options"
          onKeyDown={(e) => {
            if (e.key !== 'Escape') return
            e.stopPropagation()
            setOpen(false)
          }}
        >
          {children}
        </div>
      )}
    </span>
  )
}
