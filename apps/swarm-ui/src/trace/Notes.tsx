// What the transcript read says about itself, drawn beside the trace: the
// stream's own status when there is no event to draw, the server's notes on
// the window (a capture cut at its cap, lines that did not parse, a window
// that starts part-way, a run that recorded only its result), and the read's
// facts (source and age, format, masked, the object and its copy). The steps
// view that the trace replaced said each of these; the trace keeps them, each
// fact a word or two with its sentence behind a mark.

import { ageSpan, TERMINAL_STATES, type Task, type TaskTranscript } from '../types'
import { servedAge } from '../RunFiles'
import { Mark } from '../primitives'
import { useNow } from '../useNow'
import { Button } from '../components'

/** The stream's own status, in place of the timeline, when the read holds no step to draw. */
export function StreamStatus({ t, task }: { t: TaskTranscript; task: Task }) {
  const s = t.stream
  if (s.status === 'not_applicable') {
    return (
      <p className="trc-dim trc-pad">
        <Mark kind="zero" say="This runner has no agent CLI, so it writes no transcript. Nothing is missing." /> no agent CLI
      </p>
    )
  }
  if (s.status === 'absent') {
    return TERMINAL_STATES.has(task.state) ? (
      <p className="trc-dim trc-pad">
        <Mark kind="absent" say="No transcript object exists for this attempt: nothing was uploaded, and no live tail was published." /> no transcript recorded
        {s.detail !== null && <span className="ctl-sub"> {s.detail}</span>}
      </p>
    ) : (
      <p className="trc-dim trc-pad">
        <Mark kind="pending" say="Nothing has been published for this attempt yet. The agent's output is published every few seconds while it runs." /> nothing published yet
      </p>
    )
  }
  if (s.status === 'unreadable') {
    return (
      <p className="trc-dim trc-pad">
        <Mark kind="unread" say="The transcript object exists and could not be read. Nothing here says it is empty." /> transcript not read
        {s.detail !== null && <span className="ctl-sub"> {s.detail}</span>}
      </p>
    )
  }
  if (t.steps === null) {
    return (
      <p className="trc-dim trc-pad">
        <Mark
          kind="partial"
          say={
            t.format === 'text'
              ? 'This agent writes plain text, not a stream of events, so there are no steps to draw. Agent stdout shows it as written.'
              : 'This stream did not parse into steps. Agent stdout shows it as written.'
          }
        />{' '}
        {t.format === 'text' ? 'plain text · no steps' : 'not parsed · shown in Agent stdout'}
      </p>
    )
  }
  return (
    <p className="trc-dim trc-pad">
      <Mark kind="zero" say="The stream was read and parsed, and this window holds no step." /> no steps in this window
    </p>
  )
}

/** The server's notes on this window: each says the window is less than it looks. */
export function WindowNotes({ t }: { t: TaskTranscript }) {
  const notes: { key: string; say: string; text: string }[] = []
  if (t.stream.status === 'ok' && t.stream.detail !== null) {
    notes.push({ key: 'detail', say: 'The server says this window is less than it looks, in the words beside this mark.', text: t.stream.detail })
  }
  if (t.format === 'claude-json') {
    notes.push({
      key: 'json',
      say: 'This run printed one JSON document at the end, so its turns were never recorded. Turn-by-turn steps need the agent to run with --output-format stream-json.',
      text: 'this run recorded only its final result; turn-by-turn steps need stream-json',
    })
  }
  if (t.stream.status === 'ok' && t.window_starts_mid_stream) {
    notes.push({
      key: 'mid',
      say:
        t.stream.source === 'live'
          ? 'This window starts part-way through the stream: a live tail holds only the newest part. The whole transcript is read when the attempt ends.'
          : 'This is the newest window of the finished transcript: it ends at the last step, and earlier steps are in the windows before it.',
      text: 'earlier steps are not in this window',
    })
  }
  if (t.stream.status === 'ok' && t.capture_truncated === true) {
    notes.push({
      key: 'cap',
      say: "The agent's output passed the worker's capture size cap. The capture kept the start and the end of the run and dropped the middle, with a notice where, so this transcript is not the whole run.",
      text: 'capture cut at its cap',
    })
  }
  if (t.stream.status === 'ok' && t.skipped_lines > 0) {
    const n = t.skipped_lines
    notes.push({
      key: 'skip',
      say: `${n} line${n === 1 ? '' : 's'} of the stream did not parse and are not drawn as events. Counted, never dropped silently.`,
      text: `${n} line${n === 1 ? '' : 's'} skipped`,
    })
  }
  if (notes.length === 0) return null
  return (
    <ul className="trc-notes">
      {notes.map((n) => (
        <li key={n.key}>
          <Mark kind="partial" say={n.say} /> {n.text}
        </li>
      ))}
    </ul>
  )
}

/** The read's facts, folded: source and its age, format, masked and the object with its copy. */
export function ReadFacts({ t, fetchedAt }: { t: TaskTranscript; fetchedAt: number | null }) {
  const now = useNow(1000)
  if (t.stream.status !== 'ok') return null
  const age = servedAge(t.stream, fetchedAt, now)
  return (
    <details className="ag-logmeta trc-facts">
      <summary>
        {t.stream.source === 'live' ? 'live' : t.stream.source === 'final' ? 'final' : t.stream.source === 'artifact' ? 'the artifact' : 'transcript'}
        {t.stream.source === 'live' && age !== null && <span className="ctl-sub"> · published {ageSpan(age)} ago</span>}
      </summary>
      <ul className="ctl-facts">
        {t.format !== null && (
          <li className="ctl-fact">
            <b>format</b>
            {t.format}
          </li>
        )}
        <li className="ctl-fact">
          <b>masked</b>
          <span className={`art-masked${t.redaction_count > 0 ? ' is-warn' : ''}`}>{t.redaction_count}</span>
        </li>
        <li className="ctl-fact">
          <b>masking</b>
          <span className={`art-masked${t.redaction.applied_at_read_time ? '' : ' is-warn'}`}>
            {t.redaction.applied_at_read_time ? `applied at read time · ${t.redaction.rules} rules` : 'not applied'}
          </span>
        </li>
        {t.stream.uri !== null && (
          <li className="ctl-fact arts-object">
            <b>object</b>
            <span className="mono uri" title={t.stream.uri}>
              {t.stream.uri}
            </span>
            <Button size="sm" className="copy" onClick={() => navigator.clipboard?.writeText(`gsutil cat ${t.stream.uri}`)}>
              copy gsutil
            </Button>
          </li>
        )}
      </ul>
    </details>
  )
}
