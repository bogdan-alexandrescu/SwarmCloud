import { useEffect, useState, type MouseEvent } from 'react'

import { CIcon } from './components'

const SAID_MS = 4000

/** How many of an id's last characters a short reference keeps. */
const REF_TAIL = 8

/**
 * An id cut to its prefix and its last eight characters: `task_…24cc686f2`
 * reads `task_…` + the tail. Ids share their `task_` / `lease_` prefix and
 * differ along their length, so the TAIL is what tells two apart -- never the
 * head (Accounts › History used to cut `task_7540b2c040…` and keep the part
 * every id shares). An id no longer than the tail is printed whole.
 */
export function shortRef(id: string): string {
  const cut = id.indexOf('_')
  const prefix = cut === -1 ? '' : id.slice(0, cut + 1)
  const rest = cut === -1 ? id : id.slice(cut + 1)
  return rest.length <= REF_TAIL ? id : `${prefix}…${rest.slice(-REF_TAIL)}`
}

/**
 * ONE TASK REFERENCE ACROSS CAPACITY (QA G5-15, 2026-10-07). Holders printed
 * a task's last 10 characters, Accounts › Holding the whole
 * `task_7e07e56d17324cc686f2`, Accounts › History a head-truncated
 * `task_7540b2c040…` -- three spellings of one id, so a reader could not match
 * a Holders row to a History row by eye.
 *
 * Now: the task's title when the screen knows it (`agentName`, the way every
 * other screen names an agent), else `task_…` and the last eight in mono. The
 * whole id is always the link's `title`, and the copy button copies it whole.
 * The link opens the agent, which is where a reader goes from "what holds
 * this" to "why is it still here".
 *
 * THE CLIPBOARD IS ASKED INSIDE THE CLICK, before any await: a browser only
 * grants `writeText` during the user's gesture. Where it is refused the status
 * says so, and the whole id is still in the link's title.
 */
export function TaskRef({ id, title }: { id: string; title?: string | null | undefined }) {
  const [said, setSaid] = useState('')
  useEffect(() => {
    if (said === '') return
    const t = setTimeout(() => setSaid(''), SAID_MS)
    return () => clearTimeout(t)
  }, [said])
  const named = title !== undefined && title !== null && title !== '' && title !== id
  const copy = (e: MouseEvent) => {
    e.stopPropagation()
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined || typeof clip.writeText !== 'function') {
      setSaid('copy refused')
      return
    }
    clip.writeText(id).then(
      () => setSaid('task id copied'),
      () => setSaid('copy refused'),
    )
  }
  return (
    <span className="task-ref">
      <a
        className={named ? 'ctl-link task-ref-link' : 'ctl-link task-ref-link mono'}
        href={`#work/task/${encodeURIComponent(id)}`}
        title={id}
      >
        {named ? title : shortRef(id)}
      </a>
      <button type="button" className="task-ref-copy" title={`${id} (click to copy)`} aria-label={`Copy task id ${id}`} onClick={copy}>
        <CIcon name="copy" />
      </button>
      {said !== '' && (
        <span className="task-ref-said" role="status">
          {said}
        </span>
      )}
    </span>
  )
}

/**
 * A lease, labelled as one (QA G5-15): Holders' second line under the task was
 * an unlabelled id tail, read as a second task id. `lease …` and the same last
 * eight, the whole id in its title.
 */
export function LeaseRef({ id }: { id: string }) {
  return (
    <span className="ctl-sub lease-ref" title={id}>
      lease <span className="mono">…{id.slice(-REF_TAIL)}</span>
    </span>
  )
}
