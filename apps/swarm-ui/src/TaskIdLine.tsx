import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'

import { CIcon } from './components'

const SAID_MS = 4000

/** How many of the id's last characters a cut line always keeps. */
const TAIL = 6

/**
 * THE TASK ID, PRINTED UNDER THE AGENT'S NAME, WITH ITS COPY (#94).
 *
 * #94 named an agent by its step and kept the id as "a copyable sub-line";
 * the id had ended up only in a copy button's tooltip in the split header and
 * in a hover title on a list row, so reading one off the screen meant
 * hovering for it. One component for the Agents list row and the split
 * header, so the two never print an agent's id two ways.
 *
 * CUT IN THE MIDDLE, NEVER AT THE END. On a phone the line is narrower than
 * the id. Ids share their `task_` prefix and differ along their length, so an
 * end-ellipsis hides the part that tells two apart; the head gives way and
 * the last characters stay (styles/agents.css `.tid-*`). It is one text run
 * either way, so selecting it selects the whole id, and the button copies the
 * whole id whatever is drawn.
 *
 * THE CLIPBOARD IS ASKED INSIDE THE CLICK, before any await: a browser only
 * grants `writeText` during the user's gesture. `navigator.clipboard` is
 * undefined outside a secure context and its promise rejects when permission
 * is refused; either way the id is SELECTED, so one ⌘C / Ctrl+C still copies
 * it, and the status says which happened.
 *
 * INSIDE A LIST ROW, which is itself a button that opens the agent: a click
 * or an Enter on the copy is the copy's, never the row's.
 */
export function TaskIdLine({ id, className }: { id: string; className?: string }) {
  const text = useRef<HTMLSpanElement>(null)
  const [said, setSaid] = useState('')
  useEffect(() => {
    if (said === '') return
    const t = setTimeout(() => setSaid(''), SAID_MS)
    return () => clearTimeout(t)
  }, [said])
  const select = () => {
    const el = text.current
    const sel = typeof window === 'undefined' ? null : window.getSelection()
    if (el === null || sel === null) {
      setSaid('copy refused')
      return
    }
    const range = el.ownerDocument.createRange()
    range.selectNodeContents(el)
    sel.removeAllRanges()
    sel.addRange(range)
    setSaid('id selected: press ⌘C or Ctrl+C')
  }
  const copy = (e: MouseEvent) => {
    e.stopPropagation()
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clipboard === undefined || typeof clipboard.writeText !== 'function') {
      select()
      return
    }
    clipboard.writeText(id).then(
      () => setSaid('task id copied'),
      () => select(),
    )
  }
  // The row opens on Enter and Space; on the copy they are the copy's.
  const keep = (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') e.stopPropagation()
  }
  const cut = Math.max(0, id.length - TAIL)
  return (
    <span className={className === undefined ? 'tid' : `${className} tid`}>
      <span className="tid-text" ref={text}>
        <span className="tid-head">{id.slice(0, cut)}</span>
        <span className="tid-tail">{id.slice(cut)}</span>
      </span>
      <button
        type="button"
        className="tid-copy"
        title={`${id} (click to copy)`}
        aria-label={`Copy task id ${id}`}
        onClick={copy}
        onKeyDown={keep}
      >
        <CIcon name="copy" />
      </button>
      <span className="tid-said" role="status">
        {said}
      </span>
    </span>
  )
}
