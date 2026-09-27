import { CONCURRENCY_STATES, TERMINAL_STATES, type Task, type TaskEvent } from './types'

/**
 * The liveness badge. Shared, so it is defined once.
 *
 * THERE IS NO EXPOSED LIVENESS SIGNAL FINER THAN THE EVENT STREAM. The lease
 * document is refreshed every 30s and would give 30s resolution, but no route
 * reads it. So this is derived client-side from the newest event's timestamp,
 * and the caption says so every time — a badge implying 30s freshness when it
 * has ~150s resolution is a confident wrong answer during an incident.
 *
 * The dot is never the only carrier of meaning. The word is always present,
 * because a colour-only badge fails for colour-blind operators and in a
 * screenshot pasted into an incident channel. The dot is `.ctl-dot`, so the
 * SHAPE differs by tone as well as the colour.
 */
export type Liveness = 'live' | 'quiet' | 'silent' | 'finished' | 'not-started'

const CAPTION =
  'Derived from the newest task event, so resolution is ~150s. This is not the lease heartbeat (30s) — that is not exposed by any route.'

/**
 * THE WORD, THE FIGURE, AND THE SENTENCE -- three things, in three places.
 *
 * `copy` is what sits in the drawer's heading, and it is a FIGURE: `4m ago`,
 * `events not read`. It used to be the sentence -- "No event for 3m. Heartbeat
 * events are only every ~150s, so this is not yet alarming." is seventeen
 * words in an `<h2>`, beside a state chip and a `?` (design-system.md §12.5).
 * `word` already says whether that age is alarming; the reason it is or is not
 * is what §8.4 sends behind the `?`, and this badge's accessible name.
 *
 * `say` is the sentence, whole, and it is the badge's `aria-label`, so a
 * keyboard and a screen reader reach it without the hover a `title=` needs.
 * The `title` stays for the pointer and still carries the resolution caveat.
 */
/**
 * The `.ctl-dot` tone each reading takes -- the bare state dot of
 * design-system.md §6.6, the SAME seven silhouettes the state chip beside this
 * badge draws, not a private dot of its own.
 *
 * `unknown` is the default hollow ring and it is the honest answer twice: work
 * not dispatched has no worker to be live, and an event page nobody could
 * read says nothing either way. It was the caution colour for the second of
 * those, which painted "we could not look" as "it has gone quiet".
 */
export type LivenessTone = 'ok' | 'warn' | 'bad' | 'unknown'

export function livenessOf(
  task: Task,
  events: TaskEvent[] | null,
  now: number,
): { kind: Liveness; word: string; copy: string; say: string; tone: LivenessTone } {
  if (TERMINAL_STATES.has(task.state)) {
    // NO WALL-CLOCK TIME. This said `done at 8:46:38 PM` -- a time with no
    // date, beside a state chip that already said the task was over. The
    // badge draws nothing for a finished task (below); this answer is kept so
    // the function stays total for any other reader of it.
    return { kind: 'finished', word: 'done', copy: '', say: 'Finished.', tone: 'unknown' }
  }
  if (!CONCURRENCY_STATES.has(task.state)) {
    return {
      kind: 'not-started',
      word: 'queued',
      copy: 'not dispatched',
      say: 'Not dispatched yet, so there is no worker to be live or silent.',
      tone: 'unknown',
    }
  }

  // Newest event. A failed event read must NOT read as silence -- "we could
  // not look" and "nothing happened" are the two things this whole app exists
  // to keep apart.
  if (events === null) {
    return {
      kind: 'quiet',
      word: 'unknown',
      copy: 'events not read',
      say: 'The event history could not be read, so liveness is unknown. This is not evidence the worker is gone.',
      tone: 'unknown',
    }
  }
  const newest = events.reduce<number>((max, e) => {
    const t = new Date(e.at).getTime()
    return Number.isFinite(t) && t > max ? t : max
  }, 0)
  if (newest === 0) {
    return {
      kind: 'quiet',
      word: 'unknown',
      copy: 'no timestamp',
      say: 'No event carried a readable timestamp, so liveness cannot be derived.',
      tone: 'unknown',
    }
  }

  const ageS = Math.max(0, Math.round((now - newest) / 1000))
  const mins = Math.round(ageS / 60)

  if (ageS < 180) {
    return { kind: 'live', word: 'live', copy: `${ageS}s ago`, say: `Last event ${ageS}s ago.`, tone: 'ok' }
  }
  if (ageS < 420) {
    return {
      kind: 'quiet',
      word: 'quiet',
      copy: `${mins}m ago`,
      say: `No event for ${mins}m. Heartbeat events are only every ~150s, so this is not yet alarming.`,
      tone: 'warn',
    }
  }
  return {
    kind: 'silent',
    word: 'silent',
    copy: `${mins}m ago`,
    say: `No event for ${mins}m. The worker may be gone; the reconciler reclaims a stale lease.`,
    tone: 'bad',
  }
}

/**
 * THE BADGE, IN THE CONSOLE'S OWN VOCABULARY (AG-18).
 *
 * It drew its own 8px dot and painted its WORD in the verdict hue -- `live` in
 * green, `silent` in red -- one inch from the state chip's `.ctl-chip` mark and
 * full-ink word. Two dot vocabularies on one line, and a word whose meaning
 * was carried by its colour. It is the chip's mark now (`.ctl-dot`, the same
 * silhouettes: a disc, the caution triangle, the failure diamond, the unknown
 * ring) and the word is at full ink, as §6.6 makes every state word.
 *
 * The wrapper keeps `.liveness` for layout and drops the kind modifier: the
 * `.liveness.<kind>` rules are what coloured the word, so the badge no longer
 * matches them whether or not the sheet still carries them.
 *
 * NOTHING FOR A FINISHED TASK. "done at 8:46:38 PM" repeated what the chip
 * says -- the task is over -- and added a clock time with no date. A finished
 * task has no worker whose liveness could be in question.
 */
export function LivenessBadge({
  task,
  events,
  now,
}: {
  task: Task
  events: TaskEvent[] | null
  now: number
}) {
  const l = livenessOf(task, events, now)
  if (l.kind === 'finished') return null
  return (
    // `role="img"` so the label is the badge's accessible name -- the same
    // pattern `Mark` uses: the glass carries the fact, the name the sentence.
    <span
      className="liveness"
      data-liveness={l.kind}
      role="img"
      aria-label={`${l.word}: ${l.say} ${CAPTION}`}
      title={`${l.say}\n\n${CAPTION}`}
    >
      {/* A <span>, not the <i> the old dot was: `.liveness > i` in the sheet
          paints any italic child an 8px faint disc, which would fill the
          unknown ring in. Out of that selector's reach, the mark is exactly
          `.ctl-dot`'s, whatever the sheet still carries for the old badge. */}
      <span className={`ctl-dot is-${l.tone}`} aria-hidden />
      <span className="lv-word">{l.word}</span>
      {l.copy !== '' && <span className="lv-copy">{l.copy}</span>}
    </span>
  )
}
