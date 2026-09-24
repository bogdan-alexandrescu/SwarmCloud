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
 * screenshot pasted into an incident channel.
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
export function livenessOf(
  task: Task,
  events: TaskEvent[] | null,
  now: number,
): { kind: Liveness; word: string; copy: string; say: string } {
  if (TERMINAL_STATES.has(task.state)) {
    const at = task.completed_at ? new Date(task.completed_at).toLocaleTimeString() : null
    return {
      kind: 'finished',
      word: 'done',
      copy: at ? `at ${at}` : '',
      say: at ? `Finished at ${at}.` : 'Finished.',
    }
  }
  if (!CONCURRENCY_STATES.has(task.state)) {
    return {
      kind: 'not-started',
      word: 'queued',
      copy: 'not dispatched',
      say: 'Not dispatched yet, so there is no worker to be live or silent.',
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
    }
  }

  const ageS = Math.max(0, Math.round((now - newest) / 1000))
  const mins = Math.round(ageS / 60)

  if (ageS < 180) {
    return { kind: 'live', word: 'live', copy: `${ageS}s ago`, say: `Last event ${ageS}s ago.` }
  }
  if (ageS < 420) {
    return {
      kind: 'quiet',
      word: 'quiet',
      copy: `${mins}m ago`,
      say: `No event for ${mins}m. Heartbeat events are only every ~150s, so this is not yet alarming.`,
    }
  }
  return {
    kind: 'silent',
    word: 'silent',
    copy: `${mins}m ago`,
    say: `No event for ${mins}m. The worker may be gone; the reconciler reclaims a stale lease.`,
  }
}

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
  return (
    // `role="img"` so the label is the badge's accessible name -- the same
    // pattern `Mark` uses: the glass carries the fact, the name the sentence.
    <span
      className={`liveness ${l.kind}`}
      role="img"
      aria-label={`${l.word}: ${l.say} ${CAPTION}`}
      title={`${l.say}\n\n${CAPTION}`}
    >
      <i aria-hidden />
      <span className="lv-word">{l.word}</span>
      {l.copy !== '' && <span className="lv-copy">{l.copy}</span>}
    </span>
  )
}
