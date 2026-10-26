/**
 * WHETHER A BROWSER RUNTIME USED ITS BROWSER (lane BROWSER-USE-UI, part of
 * #2322), drawn from what lane BROWSER-USE-FLAG records: `task.browser_used`
 * and `task.browser_use`, the worker's verdict lifted out of `result_summary`
 * (`codec.browser_use_to_api`).
 *
 * WHY IT IS DRAWN AT ALL. The 2026-10-25 audit of tenant eng found browser
 * pods that never touched their browser, and found them only by reading
 * agent logs by hand. BGD decided that every task records whether its browser
 * was used and that unused browser time is visible, so the Agents list and the
 * inspector say it on the row, and Platform counts sums it.
 *
 * THREE STATES AND NOTHING. On a browser runtime the row says `browser used`
 * (its calls and images on hover in the list, on a press in the inspector),
 * `browser not used` in the warning tone, or `browser —` with its reason. A
 * runtime with no browser draws nothing: "not used" there would be every
 * claude-code task flagged for a browser it never had.
 *
 * NULL IS NEVER "NOT USED". The worker writes null when it could not read the
 * counts (no report, an unread stream, a cut stream that shows no call -- a
 * lower bound of zero is not a measurement), and the API serves null for an
 * agent not ended or one that ended before the record existed. Each is a dash
 * with its own reason; only an explicit `false` is the warning.
 */
import { useId, useState } from 'react'

import { ToneMark } from './components'
import { LIVE_BROWSER_PROFILES } from './Handoff'
import { TERMINAL_STATES, formatDuration, pluralise, type BrowserUse, type Task } from './types'

export type BrowserUseKind = 'used' | 'unused' | 'unknown'

export interface BrowserUseView {
  kind: BrowserUseKind
  /** The words on the row: `browser used`, `browser not used`, `browser`. */
  word: string
  /** The counts, or the reason there is no verdict. */
  detail: string
  /** The tone `ToneMark` draws. */
  tone: 'ok' | 'warn' | 'unknown'
  /** False only for an agent that has not ended: the list leaves those out. */
  ended: boolean
}

/** The worker's reason words (`lifecycle.browser_use_record`), as a reader says them. */
export const UNKNOWN_BECAUSE: Readonly<Record<NonNullable<BrowserUse['unknown_because']>, string>> = {
  not_reported: 'not recorded: the runner reported no browser counts',
  stream_unread: 'not known: the agent’s output could not be read',
  stream_cut: 'not known: the agent’s output was cut short and shows no browser call, which is not a measured zero',
}

const NOT_ENDED = 'recorded when the agent ends'
const NOT_RECORDED = 'not recorded: this agent ended before browser use was recorded, or the API did not send it'

/** `n noun`, or `noun not read` for a null count. */
function count(n: number | null, noun: string): string {
  return n === null ? `${noun}s not read` : pluralise(n, noun)
}

/** The counts, one line: calls (and their kinds), images, time on the runtime. */
export function browserUseCounts(use: BrowserUse): string {
  const calls = count(use.tool_calls, 'browser call')
  const kinds =
    use.tool_calls !== null && use.tool_calls > 0 && use.mcp_calls !== null && use.command_calls !== null
      ? ` (${use.mcp_calls} tool, ${use.command_calls} command)`
      : ''
  const parts = [`${calls}${kinds}`, count(use.images, 'image')]
  if (use.seconds !== null) parts.push(`${formatDuration(use.seconds * 1000)} on the browser runtime`)
  return parts.join(' · ')
}

/**
 * What the row draws for a task's browser use, or null for a task on a
 * runtime with no browser. A served verdict or detail makes it a browser
 * runtime whatever its profile is called, so a profile renamed on the server
 * is not silently dropped here.
 */
export function browserUseOf(
  task: Pick<Task, 'runner_profile' | 'state' | 'browser_used' | 'browser_use'>,
): BrowserUseView | null {
  const used = typeof task.browser_used === 'boolean' ? task.browser_used : null
  const use = task.browser_use ?? null
  if (used === null && use === null && !LIVE_BROWSER_PROFILES.has(task.runner_profile)) return null
  const ended = TERMINAL_STATES.has(task.state)
  if (used === true) {
    return { kind: 'used', word: 'browser used', tone: 'ok', ended, detail: use === null ? 'counts not sent' : browserUseCounts(use) }
  }
  if (used === false) {
    return {
      kind: 'unused',
      word: 'browser not used',
      tone: 'warn',
      ended,
      detail: use === null ? 'no browser call and no image' : browserUseCounts(use),
    }
  }
  const because = use?.unknown_because ?? null
  const detail = because !== null ? UNKNOWN_BECAUSE[because] : ended ? NOT_RECORDED : NOT_ENDED
  return { kind: 'unknown', word: 'browser', tone: 'unknown', ended, detail }
}

/** The row's words and reason, for an accessible name: `browser not used: 0 browser calls · …`. */
export function browserUseSay(view: BrowserUseView): string {
  return `${view.kind === 'unknown' ? 'browser use' : view.word}: ${view.detail}`
}

/** The mark and its words; a null verdict is the word and a dash. */
function Words({ view }: { view: BrowserUseView }) {
  return (
    <ToneMark tone={view.tone}>
      {view.kind === 'unknown' ? (
        <>
          {view.word} <span className="ctl-em">—</span>
        </>
      ) : (
        view.word
      )}
    </ToneMark>
  )
}

/**
 * THE LIST ROW'S MARK. Not a control: the row is one opener (QA-032), and a
 * button inside it would be a nested interactive. The counts are its hover,
 * and the row's accessible name carries them (`browserUseSay`), so a keyboard
 * or a phone reader has them without the hover.
 */
export function BrowserUseMark({ view }: { view: BrowserUseView }) {
  return (
    <span className={`bu-mark is-${view.kind}`} data-browser-use={view.kind} title={browserUseSay(view)}>
      <Words view={view} />
    </span>
  )
}

/**
 * THE INSPECTOR'S FACT: the same mark as a disclosure, so the counts open on
 * a press, a tap or Enter as well as on hover. Closed, it is the one word on
 * the head's line of facts.
 */
export function BrowserUseFact({ view }: { view: BrowserUseView }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <span className={`bu-fact is-${view.kind}`} data-browser-use={view.kind}>
      <button
        type="button"
        className="bu-toggle"
        aria-expanded={open}
        aria-controls={id}
        title={browserUseSay(view)}
        onClick={() => setOpen((o) => !o)}
      >
        <Words view={view} />
      </button>
      <span id={id} className="bu-detail" hidden={!open}>
        {view.detail}
      </span>
    </span>
  )
}
