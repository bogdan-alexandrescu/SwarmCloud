import { DECLARED_WORDS, taskInputsOf, type TaskInputRow } from './dag'
import { Id } from './Shell'
import { bytesLabel, type Task } from './types'

/**
 * WHAT LANDED IN THIS RUN'S WORKSPACE, AND WHERE EACH FILE CAME FROM
 * (redesign-v2 Panel 3, "Inputs -- buildable").
 *
 * `result_summary.staged_inputs` is what the worker actually put in front of
 * the agent, and each entry names the upstream TASK it came from -- "a direct
 * edge back into the workflow DAG". The board draws that edge between two
 * steps; this is its other end, on the run a reader opens from a node: every
 * file, a link back to the run that produced it (or `submission`, viz #9's case
 * for an entry with no task), and its size.
 *
 * A DECLARED FILE NOTHING HAS REPORTED IS NOT A MISSING FILE. Staging happens
 * when the run starts and the report is written when it finishes, so a live
 * run's inputs are `reported at finish`; a finished run's result that lists no
 * such file is `not reported`; a result holding entries this reader could not
 * read is `report unreadable`. Those are `DECLARED_WORDS`, the same words the
 * graph's edges and the workflow table use, decided once by `declaredWhy`.
 *
 * NOTHING AT ALL for a run that declared no input and reported none, which is
 * every run that is not a workflow step with `input_from`. That is not an
 * absence -- nothing was asked for -- and a heading over an empty list on every
 * drawer would be the chrome §6.11 keeps taking off this screen.
 *
 * No prose on the surface (redesign-v2 §9) and no `?` (help-density.md): the
 * facts are cells, the not-yets are words on the absence rule, and each word's
 * sentence is its accessible name.
 */
export function StagedInputs({ task }: { task: Task }) {
  const { rows, malformed } = taskInputsOf(task)
  if (rows.length === 0 && malformed === 0) return null
  return (
    <div className="section run-inputs">
      <span className="ctl-eyebrow">staged inputs</span>
      {rows.length > 0 && (
        <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">File</th>
                <th role="columnheader" scope="col">From</th>
                <th role="columnheader" scope="col" className="is-num">Arrived</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((r, i) => (
                <tr role="row" key={`${r.file}-${i}`} data-file={r.file}>
                  <th role="rowheader" scope="row" className="mono">
                    {r.file}
                  </th>
                  <td role="cell" data-label="From">
                    <From row={r} />
                  </td>
                  <td role="cell" data-label="Arrived" className="is-num">
                    <Arrived row={r} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {malformed > 0 && (
        <span
          className="ctl-mark is-unread"
          aria-label={`${malformed} entr${malformed === 1 ? 'y' : 'ies'} in this run's staged-input report could not be read as a file. Counted here rather than dropped.`}
        >
          {malformed} unreadable
        </span>
      )}
    </div>
  )
}

/** The upstream run as a link -- the edge back into the graph -- or the submission. */
function From({ row }: { row: TaskInputRow }) {
  if (row.from.kind === 'submission') return <>submission</>
  return (
    <>
      <a href={`#work/task/${encodeURIComponent(row.from.taskId)}`}>
        <Id title={row.from.taskId}>{row.from.taskId}</Id>
      </a>
      {/* A file that arrived from a run this one did not declare an input
          from. `validation.py` should make it impossible for a workflow step;
          it is listed rather than hidden, and marked, because a file in the
          workspace that appears nowhere is the silent drop this console
          refuses. */}
      {!row.declared && <span className="ctl-sub">not declared</span>}
    </>
  )
}

/** Its size when a report says it arrived; otherwise which kind of not-yet it is. */
function Arrived({ row }: { row: TaskInputRow }) {
  const a = row.arrival
  if (a.kind === 'declared') {
    const w = DECLARED_WORDS[a.why]
    return (
      <span className="ctl-mark is-absent" aria-label={w.note}>
        {w.text}
      </span>
    )
  }
  return (
    <>
      {a.bytes === null ? (
        <span className="ctl-mark is-absent" aria-label="The report names this file but carries no size for it. Not a zero.">
          size not reported
        </span>
      ) : (
        bytesLabel(a.bytes)
      )}
      {/* The restored checkpoint already held it, so this attempt did not fetch it again. */}
      {a.fromCheckpoint && <span className="ctl-sub">from checkpoint</span>}
    </>
  )
}
