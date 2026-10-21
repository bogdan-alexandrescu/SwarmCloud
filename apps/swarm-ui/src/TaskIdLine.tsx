import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'

import { CIcon } from './components'

const SAID_MS = 4000

/**
 * THE TASK ID, PRINTED UNDER THE AGENT'S NAME, WITH ITS COPY (#94).
 *
 * #94 named an agent by its step and kept the id as "a copyable sub-line";
 * the id had ended up only in a copy button's tooltip in the split header and
 * in a hover title on a list row, so reading one off the screen meant
 * hovering for it. One component for the Agents list row and the split
 * header, so the two never print an agent's id two ways.
 *
 * CUT AT ITS START, NEVER AT ITS END. On a phone the line is narrower than
 * the id. Ids share their `task_` prefix and differ along their length, so an
 * end-ellipsis hides the part that tells two apart; the start gives way and
 * the end stays (styles/agents.css `.tid-text`).
 *
 * ONE TEXT NODE (visual QA run 2, task-017). The cut was in the middle, drawn
 * as a `.tid-head` and a `.tid-tail` span in a flex box, which made the two
 * halves two blocks: a screen reader read `task_a0` and `73aff5` as two
 * words and a plain copy carried a line break between them. The id is now
 * one run of text, so reading it, selecting it and copying it all give the
 * whole id; the button copies the whole id whatever is drawn.
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
  return (
    <span className={className === undefined ? 'tid' : `${className} tid`}>
      <span className="tid-text" ref={text}>
        {id}
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
