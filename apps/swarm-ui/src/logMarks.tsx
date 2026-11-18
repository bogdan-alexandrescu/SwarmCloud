import { createContext, useContext } from 'react'

import { bytePos, gapSize, isErrorLine, linesOf, segments, type Gap, type GapUnknown } from './logLines'
import type { TranscriptStep } from './types'

/**
 * WHAT THE LOG DOCK ASKS EVERY LINE TO MARK (viewers.html A): the search's
 * hits, the lines the error pattern matches, the one the reader jumped to,
 * whether lines wrap, and a gap in a stream's tail. The dock provides it; the
 * views in Artifacts.tsx read it. Outside the dock nothing is provided, and
 * the default marks nothing -- so a view rendered anywhere else draws exactly
 * what it drew before.
 *
 * THE DOM IS THE COUNT. A line marks itself `is-hit` / `is-err` and carries
 * `data-log-key`; the dock counts and walks those elements in document order.
 * One rule decides what is drawn as a hit and what the counter counts, so the
 * two cannot disagree -- a transcript's tool result folded into its call's row
 * is one row, and one target, on both.
 */
export interface LogMarks {
  needle: string
  current: string | null
  wrap: boolean
  /** By stream name: what the dock found comparing this read's tail with the last. */
  gaps: Readonly<Record<string, Gap | GapUnknown | undefined>>
}

export const NO_MARKS: LogMarks = { needle: '', current: null, wrap: false, gaps: {} }

export const LogMarksContext = createContext<LogMarks>(NO_MARKS)

export function useLogMarks(): LogMarks {
  return useContext(LogMarksContext)
}

/** Everything a step says, for search: its text, its tool, its input and its result. */
export function stepText(step: TranscriptStep): string {
  return [step.text, step.tool?.name, step.tool?.input, step.tool_result?.content].filter((x) => typeof x === 'string').join('\n')
}

/**
 * A step is an error when its tool result says so, when it is the run's final
 * `result` and that result is marked `is_error` (e.g. `error_max_turns`, which
 * the transcript view draws as "the agent reported an error"), or when the
 * console-side pattern matches its text.
 */
export function stepIsError(step: TranscriptStep, result?: TranscriptStep | null): boolean {
  if (step.tool_result?.is_error === true || result?.tool_result?.is_error === true) return true
  if (step.kind === 'result' && step.meta?.is_error === true) return true
  return isErrorLine(stepText(step)) || (result !== undefined && result !== null && isErrorLine(stepText(result)))
}

/** The classes and attributes a step's row takes from the marks. */
export function stepMarks(
  marks: LogMarks,
  step: TranscriptStep,
  result?: TranscriptStep | null,
): { className: string; 'data-log-key': string } {
  const key = `step:${step.id}`
  const n = marks.needle.trim().toLowerCase()
  const text = `${stepText(step)}\n${result ? stepText(result) : ''}`.toLowerCase()
  const cls = [
    n !== '' && text.includes(n) ? 'is-hit' : '',
    stepIsError(step, result) ? 'is-err' : '',
    marks.current === key ? 'is-current' : '',
  ]
    .filter(Boolean)
    .join(' ')
  return { className: cls, 'data-log-key': key }
}

/**
 * A STREAM'S WINDOW AS LINES: numbered, each search hit and the server's mask
 * drawn as its own run, an error line marked. Still one `<pre>`, so a reader
 * copying the window copies the window.
 */
export function LogText({ name, content }: { name: string; content: string }) {
  const marks = useLogMarks()
  const lines = linesOf(content)
  return (
    <pre className={`logwin-body ag-logtext${marks.wrap ? ' is-wrap' : ''}`}>
      {lines.map((line, i) => {
        const key = `${name}:${i}`
        const segs = segments(line, marks.needle)
        const cls = [
          'ag-logline',
          segs.some((s) => s.kind === 'hit') ? 'is-hit' : '',
          isErrorLine(line) ? 'is-err' : '',
          marks.current === key ? 'is-current' : '',
        ]
          .filter(Boolean)
          .join(' ')
        return (
          <span key={i} className={cls} data-log-key={key}>
            <span className="ag-logno" aria-hidden="true">
              {i + 1}
            </span>
            {segs.map((s, j) =>
              s.kind === 'mask' ? (
                <span key={j} className="ag-mask" title="masked by the server at read time">
                  {s.text}
                </span>
              ) : s.kind === 'hit' ? (
                <mark key={j}>{s.text}</mark>
              ) : (
                <Fragmentless key={j} text={s.text} />
              ),
            )}
            {'\n'}
          </span>
        )
      })}
    </pre>
  )
}

function Fragmentless({ text }: { text: string }) {
  return <>{text}</>
}

function clock(at: string | null): string | null {
  if (at === null) return null
  const t = Date.parse(at)
  return Number.isFinite(t) ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : null
}

/**
 * OUTPUT MISSING BETWEEN TWO READS (viewers.html A): amber and dashed, at the
 * top of the window it precedes, never merged into the lines around it. Both
 * bytes and both publish times are the server's. A tail with no position
 * header says the console cannot tell, which is not "nothing is missing".
 */
export function GapNotice({ name }: { name: string }) {
  const gap = useLogMarks().gaps[name]
  if (gap === undefined) return null
  if (gap.kind === 'unknown') {
    return (
      <p className="ag-gap is-unknown" role="note">
        Cannot tell whether output is missing between the last two reads of <span className="mono">{name}</span>: this
        live tail carries no position header, so the reads cannot be compared.
      </p>
    )
  }
  const before = clock(gap.before)
  const after = clock(gap.after)
  return (
    <p className="ag-gap" role="note">
      Output missing between byte {bytePos(gap.from)} and {bytePos(gap.to)} ({gapSize(gap.to - gap.from)}
      {before !== null && after !== null ? `, published ${before} → ${after}` : ''}). The live tail holds only the newest part of
      the stream, and it moved past these bytes between two reads. The final log will hold them once the attempt ends.
    </p>
  )
}
